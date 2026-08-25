from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import text

from meta_research.composition import build_production_runtime
from meta_research.experiment import (
    ExperimentIntent,
    ExperimentObservation,
    ExperimentProviderRequest,
    ExperimentProviderResult,
    ExperimentRuntimeBinding,
    ExperimentService,
)
from meta_research.experiment_contract import (
    PROTOCOL_EXPERIMENT_RESULT_SCHEMA,
    ProtocolExperimentIntent,
    experiment_definition_document,
    experiment_execution_log_document,
)
from meta_research.owners.common import OwnerConflict, canonical_json
from meta_research.owners.research_memory import AssetIntakeRequest
from meta_research.paths import prepare_data_root
from meta_research.quest_drafting import (
    HostComputeDevice,
    HostComputeSnapshot,
    IntentTurnRequest,
    IntentTurnResult,
    ProposalDraftRequest,
    ProposalDraftResult,
)


_QUESTION = {
    "title": "微型实验能否保持完整测量身份",
    "unknown_statement": "尚不明确变体偏移对固定样本的整体均值影响。",
    "answer_shape": "形成可复核的完整测量结果。",
    "applicability_scope": "本机内置微型数值实验。",
    "background_context": "用于验证生产实验纵向切片。",
    "requirements_constraints": "不得把执行完成冒充 Formal Measurement。",
}


class _DeterministicDraftingAdapter:
    def draft(self, request: ProposalDraftRequest) -> ProposalDraftResult:
        return ProposalDraftResult(_QUESTION, "test_deterministic")

    def reply(self, request: IntentTurnRequest) -> IntentTurnResult:
        return IntentTurnResult(
            "测试回复",
            request.native_session_ref or "intent-session",
            "test_deterministic",
        )


class _DeterministicProbe:
    def observe(self) -> HostComputeSnapshot:
        return HostComputeSnapshot(
            status="ready",
            observed_at=1720000000.0,
            devices=(
                HostComputeDevice(
                    uuid="GPU-experiment-test",
                    name="Experiment Test GPU",
                    memory_total_mib=81920,
                ),
            ),
            adapter_kind="test_probe",
        )


class _DeterministicExperimentProvider:
    _IMPLEMENTATION_BUNDLE = b"test-experiment-provider-v1"

    def __init__(self) -> None:
        self.runtime_binding_calls = 0
        self.implementation_bundle_calls = 0
        self.execute_calls = 0
        self.requests: list[ExperimentProviderRequest] = []

    def implementation_bundle(self) -> bytes:
        self.implementation_bundle_calls += 1
        return self._IMPLEMENTATION_BUNDLE

    def runtime_binding(self) -> ExperimentRuntimeBinding:
        self.runtime_binding_calls += 1
        return ExperimentRuntimeBinding(
            runner_bundle_hash=hashlib.sha256(
                self._IMPLEMENTATION_BUNDLE
            ).hexdigest(),
            adapter_ref="test-experiment-provider-v1",
            interpreter_ref="python-test",
            capability_bindings=("subprocess",),
            resource_bindings=("host:test",),
        )

    def execute(
        self,
        request: ExperimentProviderRequest,
        observe,
    ) -> ExperimentProviderResult:
        self.execute_calls += 1
        self.requests.append(request)
        selected_values = []
        for checkpoint in request.selected_checkpoints:
            checkpoint_value = json.loads(checkpoint.content.decode("utf-8"))
            selected_values.append(float(checkpoint_value["weights"][0]))
        variant_mean = (
            sum(selected_values) / len(selected_values)
            if selected_values
            else -0.25
        )
        observe(
            ExperimentObservation(
                kind="stdout",
                payload={"line": "state formation complete", "stream": "stdout"},
                observed_at=1_720_000_001.0,
            )
        )
        observe(
            ExperimentObservation(
                kind="telemetry",
                payload={
                    "collector": "test-telemetry-v1",
                    "device": "host:test",
                    "scope": "host-wide; correlated, not exclusive",
                    "correlation": "same execution fence",
                    "cadence_seconds": 1.0,
                    "stale_after_seconds": 5.0,
                    "sample_time": 1_720_000_001.0,
                    "freshness": "live",
                    "cpu_load": {
                        "value": 0.25,
                        "unit": "ratio",
                        "denominator": "host logical CPUs",
                    },
                },
                observed_at=1_720_000_001.0,
            )
        )
        return ExperimentProviderResult(
            checkpoint_content=(
                canonical_json({"weights": [variant_mean]}).encode("utf-8")
                if request.request_kind == "retrain"
                else None
            ),
            analysis={
                "direction": "negative",
                "interpretation": "valid negative result",
            },
            result_content={
                "schema_ref": "meta-research/micro-experiment-result/v1",
                "metrics": {
                    "baseline_mean": 0.0,
                    "variant_mean": variant_mean,
                    "mean_delta": variant_mean,
                },
                "aggregation": "single fixed sample set; arithmetic mean",
            },
            adapter_kind="test_deterministic",
        )


class _DeclaredOnlyExperimentProvider:
    def __init__(self) -> None:
        self.execute_calls = 0

    def runtime_binding(self) -> ExperimentRuntimeBinding:
        return ExperimentRuntimeBinding(
            runner_bundle_hash=hashlib.sha256(b"unavailable-bundle").hexdigest(),
            adapter_ref="declared-only-provider",
            interpreter_ref="python-test",
            capability_bindings=("subprocess",),
            resource_bindings=("host:test",),
        )

    def execute(self, request, observe) -> ExperimentProviderResult:
        self.execute_calls += 1
        raise AssertionError("provider without implementation bytes must not execute")


class _MultipleCheckpointProvider(_DeterministicExperimentProvider):
    def execute(self, request, observe) -> ExperimentProviderResult:
        result = super().execute(request, observe)
        return replace(
            result,
            additional_checkpoint_contents=(
                (
                    b'{"weights":[-0.125]}',
                    b'{"weights":[-0.0625]}',
                )
                if request.request_kind == "retrain"
                else ()
            ),
        )


class _MetricsExperimentProvider(_DeterministicExperimentProvider):
    def __init__(self, metrics: dict[str, float]) -> None:
        super().__init__()
        self._metrics = metrics

    def execute(self, request, observe) -> ExperimentProviderResult:
        result = super().execute(request, observe)
        return replace(
            result,
            result_content={**result.result_content, "metrics": self._metrics},
        )


class _SequenceMetricsProvider(_DeterministicExperimentProvider):
    def __init__(self, metrics: tuple[dict[str, float], ...]) -> None:
        super().__init__()
        self._metrics = metrics
        self._index = 0

    def execute(self, request, observe) -> ExperimentProviderResult:
        result = super().execute(request, observe)
        metrics = self._metrics[self._index]
        self._index += 1
        return replace(
            result,
            result_content={**result.result_content, "metrics": metrics},
        )


class _ProtocolEvaluationProvider(_DeterministicExperimentProvider):
    def __init__(self, metrics: dict[str, float]) -> None:
        super().__init__()
        self._metrics = metrics

    def execute(self, request, observe) -> ExperimentProviderResult:
        self.execute_calls += 1
        self.requests.append(request)
        assert request.definition is not None
        assert request.definition["schema_ref"] == (
            "meta-research/protocol-experiment-definition/v2"
        )
        assert request.definition["execution"] == {
            "adapter_kind": "test_rule_protocol",
            "schema_ref": "test/rule-protocol/v1",
            "payload": {"rule": "compare-normalized-sets"},
        }
        observe(
            ExperimentObservation(
                kind="stdout",
                payload={"line": "rule protocol completed", "stream": "stdout"},
                observed_at=1_720_000_101.0,
            )
        )
        return ExperimentProviderResult(
            checkpoint_content=None,
            analysis={"interpretation": "rule protocol completed without model state"},
            result_content={
                "schema_ref": PROTOCOL_EXPERIMENT_RESULT_SCHEMA,
                "metrics": self._metrics,
                "result_disposition": "nonsignificant",
            },
            adapter_kind="test_rule_protocol",
            schema_ref=PROTOCOL_EXPERIMENT_RESULT_SCHEMA,
        )


class _CrashBeforeRuntimeAdmission:
    def admit_experiment(self, **_values):
        raise RuntimeError("simulated_crash_before_runtime_admission")


def _drain_experiments(runtime, *, limit: int = 20) -> int:
    processed = 0
    while processed < limit and runtime.experiment.process_once():
        processed += 1
    return processed


def _runtime(path: Path, experiment_provider=None):
    drafting = _DeterministicDraftingAdapter()
    return build_production_runtime(
        prepare_data_root(path),
        proposal_drafter=drafting,
        intent_drafting_provider=drafting,
        host_compute_probe=_DeterministicProbe(),
        experiment_provider=experiment_provider or _DeterministicExperimentProvider(),
    )


def _experiment_write_counts(runtime) -> dict[str, int]:
    tables = (
        "rm_asset_intakes",
        "rm_assets",
        "rm_asset_versions",
        "rg_experiment_input_bindings",
        "rg_experiment_requests",
        "rg_experiment_idempotency",
        "ar_experiment_runs",
        "ar_experiment_sessions",
        "ar_experiment_attempts",
        "ar_provider_units",
    )
    with runtime._database.read() as connection:
        return {
            table: int(
                connection.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
            )
            for table in tables
        }


def _accept_test_asset(runtime, *, name: str, content: bytes):
    intake = runtime.owners.research_memory.submit_asset_intake(
        AssetIntakeRequest(
            source_kind="text",
            custody_mode="managed",
            display_name=name,
            media_type="application/json",
            content=content,
            provenance={"kind": "formal_measurement_attack_fixture"},
        ),
        idempotency_key=f"formal-measurement-attack:{name}",
    )
    assert intake.status == "accepted"
    assert intake.asset is not None
    return intake.asset.as_binding()


def _confirm_direct_quest(runtime) -> dict[str, object]:
    human = runtime.owners.human_collaboration
    opened = human.create_quest({}, "experiment-quest-open")
    probed = human.observe_host_compute(
        opened["initialization_id"],
        ["GPU-experiment-test"],
        "experiment-compute-probe",
    )
    draft = dict(probed["quest_draft"]["value"])
    draft.update(
        {
            "goal": "验证微型真实实验的身份和完整测量接纳。",
            "completion_criteria": "一个 Attempt 独自覆盖全部必需 Metric。",
            "time_budget": "7d",
            "route": "direct",
            "literature": {
                "mode": "oa_only",
                "library_entry_url": "",
                "scope_exclusions": "",
                "accepted_material_bindings": [],
            },
            "background_and_initial_direction": "比较固定基线与数值偏移变体。",
        }
    )
    human.revise_quest_draft(
        opened["initialization_id"],
        draft,
        probed["quest_draft"]["hash"],
        "experiment-quest-draft",
        probed["quest_draft"]["revision"],
    )
    drafted = human.query_quest_creation(opened["initialization_id"])
    human.generate_question_proposal(
        opened["initialization_id"],
        drafted["quest_draft"]["hash"],
        "experiment-proposal",
        drafted["quest_draft"]["revision"],
    )
    assert human.process_drafting_once()
    proposed = human.query_quest_creation(opened["initialization_id"])
    previewed = human.preview_confirmation(
        opened["initialization_id"],
        quest_draft_revision=proposed["quest_draft"]["revision"],
        quest_draft_hash=proposed["quest_draft"]["hash"],
        proposal_ref=proposed["proposal"]["ref"],
        proposal_hash=proposed["proposal"]["hash"],
        idempotency_key="experiment-preview",
    )
    human.confirm_quest(
        opened["initialization_id"],
        quest_draft_revision=proposed["quest_draft"]["revision"],
        quest_draft_hash=proposed["quest_draft"]["hash"],
        proposal_ref=proposed["proposal"]["ref"],
        proposal_hash=proposed["proposal"]["hash"],
        preview_ref=previewed["confirmation_preview"]["ref"],
        preview_hash=previewed["confirmation_preview"]["hash"],
        idempotency_key="experiment-confirm",
    )
    for _step in range(6):
        if not human.reconcile_once():
            break
    completed = human.query_quest_creation(opened["initialization_id"])
    assert completed["status"] == "completed"
    return completed


def test_experiment_admission_keeps_domain_and_runtime_identities_independent(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path / "experiment-admission")
    try:
        quest = _confirm_direct_quest(runtime)
        intent = ExperimentIntent(
            execution_request_ref="semantic-experiment-request-one",
            quest_ref=quest["quest_ref"],
            title="固定样本偏移微实验",
            hypothesis="数值偏移会改变整体均值，但结果符号不决定能否接纳。",
            variant_parameter=-0.25,
            sample_count=16,
        )

        admitted = runtime.experiment.start(intent, "experiment-start-one")

        identities = admitted["identities"]
        assert set(identities) == {
            "baseline_ref",
            "variant_ref",
            "evaluation_protocol_ref",
            "protocol_version_ref",
            "evaluation_ref",
            "variant_run_ref",
            "evaluation_attempt_ref",
        }
        assert len(set(identities.values())) == len(identities)
        assert admitted["execution"] == {
            **admitted["execution"],
            "status": "admitted",
            "attempt_generation": 1,
            "fence_status": "current",
        }
        assert admitted["execution"]["run_ref"] not in identities.values()
        assert admitted["execution"]["attempt_ref"] not in identities.values()
        assert admitted["execution"]["root_session_ref"] not in identities.values()
        assert admitted["execution"]["fence_ref"] not in identities.values()
        assert set(admitted["frozen_inputs"]) == {
            "variant_run",
            "evaluation_attempt",
        }
        variant_run_binding = admitted["frozen_inputs"]["variant_run"]
        measurement_binding = admitted["frozen_inputs"]["evaluation_attempt"]
        assert variant_run_binding["subject_ref"] == identities["variant_run_ref"]
        assert measurement_binding["subject_ref"] == identities["evaluation_attempt_ref"]
        assert variant_run_binding["binding_ref"] != measurement_binding["binding_ref"]
        assert variant_run_binding["hash"] != measurement_binding["hash"]
        assert variant_run_binding["receipt"]["subject_ref"] == variant_run_binding["binding_ref"]
        assert measurement_binding["receipt"]["subject_ref"] == measurement_binding["binding_ref"]
        assert "target_ref" not in admitted
        assert "target_commit" not in admitted
        assert admitted["assets"] == {
            "status": "not_attempted",
            "checkpoint_artifacts": [],
            "log_assets": [],
            "analysis_assets": [],
            "result_content": None,
        }
        assert admitted["formal_measurement"] == {
            "status": "not_attempted",
            "metric_result": None,
        }

        assert runtime.experiment.start(intent, "experiment-start-one") == admitted
        assert runtime.experiment.start(intent, "experiment-start-one-retry") == admitted

        repeated = runtime.experiment.start(
            replace(
                intent,
                execution_request_ref="semantic-experiment-request-two",
            ),
            "experiment-start-two",
        )
        for reused in (
            "baseline_ref",
            "variant_ref",
            "evaluation_protocol_ref",
            "protocol_version_ref",
            "evaluation_ref",
        ):
            assert repeated["identities"][reused] == identities[reused]
        for new_identity in ("variant_run_ref", "evaluation_attempt_ref"):
            assert repeated["identities"][new_identity] != identities[new_identity]
        assert repeated["execution"]["run_ref"] != admitted["execution"]["run_ref"]
    finally:
        runtime.close()


def test_bundle_target_namespace_is_rejected_before_provider_or_owner_writes(
    tmp_path: Path,
) -> None:
    provider = _DeterministicExperimentProvider()
    runtime = _runtime(tmp_path / "bundle-target-namespace-rejected", provider)
    try:
        quest = _confirm_direct_quest(runtime)
        before_counts = _experiment_write_counts(runtime)
        before_snapshots = tuple(
            owner.query_snapshot()
            for owner in (
                runtime.owners.research_memory,
                runtime.owners.research_graph,
                runtime.owners.agent_runtime,
            )
        )
        before_provider_calls = (
            provider.runtime_binding_calls,
            provider.implementation_bundle_calls,
            provider.execute_calls,
        )

        with pytest.raises(
            OwnerConflict,
            match="bundle_target_experiment_write_forbidden",
        ):
            runtime.experiment.start(
                ExperimentIntent(
                    execution_request_ref="bundle-target-forged-source-wrapper",
                    quest_ref=quest["quest_ref"],
                    title="forged formal Target Experiment",
                    hypothesis="Formal Target must never enter Experiment authority.",
                    variant_parameter=-0.25,
                    sample_count=16,
                ),
                "reject-bundle-target-namespace",
            )

        assert (
            provider.runtime_binding_calls,
            provider.implementation_bundle_calls,
            provider.execute_calls,
        ) == before_provider_calls
        assert _experiment_write_counts(runtime) == before_counts
        assert tuple(
            owner.query_snapshot()
            for owner in (
                runtime.owners.research_memory,
                runtime.owners.research_graph,
                runtime.owners.agent_runtime,
            )
        ) == before_snapshots
    finally:
        runtime.close()


def _protocol_intent(quest_ref: str, request_ref: str) -> ProtocolExperimentIntent:
    return ProtocolExperimentIntent(
        execution_request_ref=request_ref,
        quest_ref=quest_ref,
        title="无 checkpoint 的规则协议",
        objective="按冻结规则比较两个规范化集合，并形成正式测量。",
        baseline_forward_contract={
            "schema_ref": "test/set-forward-contract/v1",
            "input": "two finite identifier sets",
            "output": "normalized identifier sets",
        },
        variant_recipe={
            "schema_ref": "test/set-rule-variant/v1",
            "operation": "normalize then compare",
        },
        evaluation_protocol_lineage={
            "schema_ref": "test/set-protocol-lineage/v1",
            "name": "set agreement",
        },
        protocol_version={
            "schema_ref": "test/set-protocol/v3",
            "required_metrics": ["agreement_rate", "conflict_count"],
            "optional_metrics": ["coverage_rate"],
            "stopping_rule": "all identifiers classified",
        },
        execution={
            "adapter_kind": "test_rule_protocol",
            "schema_ref": "test/rule-protocol/v1",
            "payload": {"rule": "compare-normalized-sets"},
        },
        checkpoint_policy="forbidden",
    )


def test_protocol_experiment_accepts_arbitrary_metrics_without_a_checkpoint(
    tmp_path: Path,
) -> None:
    provider = _ProtocolEvaluationProvider(
        {"agreement_rate": 0.875, "conflict_count": 2.0, "coverage_rate": 1.0}
    )
    runtime = _runtime(tmp_path / "protocol-experiment", provider)
    try:
        quest = _confirm_direct_quest(runtime)
        admitted = runtime.experiment.start(
            _protocol_intent(quest["quest_ref"], "protocol-experiment-request"),
            "protocol-experiment-start",
        )
        for _step in range(3):
            assert runtime.experiment.process_once()
        accepted = runtime.experiment.query(
            admitted["identities"]["evaluation_attempt_ref"]
        )
        assert accepted["assets"]["checkpoint_artifacts"] == []
        assert accepted["formal_measurement"]["status"] == "accepted"
        assert accepted["formal_measurement"]["metric_result"]["metrics"] == {
            "agreement_rate": 0.875,
            "conflict_count": 2.0,
            "coverage_rate": 1.0,
        }
        assert provider.requests[0].checkpoint_policy == "forbidden"
        assert provider.requests[0].required_metrics == (
            "agreement_rate",
            "conflict_count",
        )
    finally:
        runtime.close()


def test_protocol_experiment_rejects_a_missing_required_metric(
    tmp_path: Path,
) -> None:
    provider = _ProtocolEvaluationProvider({"agreement_rate": 0.875})
    runtime = _runtime(tmp_path / "protocol-experiment-missing-metric", provider)
    try:
        quest = _confirm_direct_quest(runtime)
        admitted = runtime.experiment.start(
            _protocol_intent(quest["quest_ref"], "protocol-missing-metric-request"),
            "protocol-missing-metric-start",
        )
        for _step in range(3):
            assert runtime.experiment.process_once()
        rejected = runtime.experiment.query(
            admitted["identities"]["evaluation_attempt_ref"]
        )
        assert rejected["formal_measurement"] == {
            "status": "rejected",
            "reason": {"code": "formal_measurement_metrics_incomplete"},
            "metric_result": None,
        }
    finally:
        runtime.close()


def test_authority_preflight_rejects_before_content_or_execution_side_effects(
    tmp_path: Path,
) -> None:
    provider = _DeterministicExperimentProvider()
    runtime = _runtime(tmp_path / "experiment-preflight", provider)
    try:
        owners = runtime.owners
        before = tuple(
            owner.query_snapshot()
            for owner in (
                owners.research_memory,
                owners.research_graph,
                owners.agent_runtime,
            )
        )
        feed_revision = runtime.feed.current_revision()
        with pytest.raises(OwnerConflict, match="experiment_quest_not_accepted"):
            runtime.experiment.start(
                ExperimentIntent(
                    execution_request_ref="preflight-invalid-quest",
                    quest_ref="quest-not-accepted",
                    title="invalid quest",
                    hypothesis="无有效 Quest 不得创建实验内容。",
                    variant_parameter=-0.25,
                    sample_count=16,
                ),
                "preflight-invalid-quest",
            )
        assert provider.runtime_binding_calls == 0
        assert provider.execute_calls == 0
        assert runtime.feed.current_revision() == feed_revision
        assert tuple(
            owner.query_snapshot()
            for owner in (
                owners.research_memory,
                owners.research_graph,
                owners.agent_runtime,
            )
        ) == before

        quest = _confirm_direct_quest(runtime)
        intent = ExperimentIntent(
            execution_request_ref="preflight-stable-request",
            quest_ref=quest["quest_ref"],
            title="stable request",
            hypothesis="同一 semantic request 不可悄然改写。",
            variant_parameter=-0.25,
            sample_count=16,
        )
        runtime.experiment.start(intent, "preflight-stable-request")
        accepted_snapshots = tuple(
            owner.query_snapshot()
            for owner in (
                owners.research_memory,
                owners.research_graph,
                owners.agent_runtime,
            )
        )
        accepted_feed_revision = runtime.feed.current_revision()
        runtime_calls = provider.runtime_binding_calls
        with pytest.raises(
            OwnerConflict, match="experiment_execution_request_conflict"
        ):
            runtime.experiment.start(
                replace(intent, title="mutated request"),
                "preflight-mutated-request",
            )
        assert provider.runtime_binding_calls == runtime_calls
        assert provider.execute_calls == 0
        assert runtime.feed.current_revision() == accepted_feed_revision
        assert tuple(
            owner.query_snapshot()
            for owner in (
                owners.research_memory,
                owners.research_graph,
                owners.agent_runtime,
            )
        ) == accepted_snapshots

        foreign_source = runtime.experiment.start(
            replace(
                intent,
                execution_request_ref="preflight-foreign-source",
                variant_parameter=0.5,
            ),
            "preflight-foreign-source",
        )
        assert _drain_experiments(runtime) == 6
        before_foreign_remeasure = tuple(
            owner.query_snapshot()
            for owner in (
                owners.research_memory,
                owners.research_graph,
                owners.agent_runtime,
            )
        )
        before_foreign_feed = runtime.feed.current_revision()
        before_foreign_runtime_calls = provider.runtime_binding_calls
        before_foreign_execute_calls = provider.execute_calls
        with pytest.raises(
            OwnerConflict, match="experiment_source_variant_run_foreign"
        ):
            runtime.experiment.start(
                replace(
                    intent,
                    execution_request_ref="preflight-foreign-remeasure",
                    request_kind="remeasure",
                    source_variant_run_ref=foreign_source["identities"][
                        "variant_run_ref"
                    ],
                ),
                "preflight-foreign-remeasure",
        )
        assert provider.runtime_binding_calls == before_foreign_runtime_calls
        assert provider.execute_calls == before_foreign_execute_calls
        assert runtime.feed.current_revision() == before_foreign_feed
        assert tuple(
            owner.query_snapshot()
            for owner in (
                owners.research_memory,
                owners.research_graph,
                owners.agent_runtime,
            )
        ) == before_foreign_remeasure
    finally:
        runtime.close()


def test_owner_admission_binds_rm_implementation_to_exact_runtime_bundle(
    tmp_path: Path,
) -> None:
    provider = _DeterministicExperimentProvider()
    runtime = _runtime(tmp_path / "experiment-implementation-binding", provider)
    try:
        quest = _confirm_direct_quest(runtime)
        intent = ExperimentIntent(
            execution_request_ref="implementation-binding-request",
            quest_ref=quest["quest_ref"],
            title="exact implementation binding",
            hypothesis="RM implementation bytes 必须与实际 runtime bundle 完全一致。",
            variant_parameter=-0.25,
            sample_count=16,
        )
        runtime_a = provider.runtime_binding()
        definition_a = experiment_definition_document(intent, runtime_a)
        definition_binding = _accept_test_asset(
            runtime,
            name="implementation-binding-definition.json",
            content=canonical_json(definition_a).encode("utf-8"),
        )
        implementation_a = _accept_test_asset(
            runtime,
            name="implementation-binding-a.json",
            content=provider.implementation_bundle(),
        )
        implementation_b_content = b"different-real-implementation-bundle"
        implementation_b = _accept_test_asset(
            runtime,
            name="implementation-binding-b.json",
            content=implementation_b_content,
        )

        graph_before = runtime.owners.research_graph.query_snapshot()
        agent_before = runtime.owners.agent_runtime.query_snapshot()
        feed_before = runtime.feed.current_revision()
        with pytest.raises(
            OwnerConflict,
            match="experiment_implementation_binding_mismatch",
        ):
            runtime.owners.research_graph.admit_experiment(
                intent=intent,
                runtime_binding=runtime_a,
                definition_binding=definition_binding,
                implementation_binding=implementation_b,
                idempotency_key="implementation-binding-mismatch",
            )
        assert runtime.owners.research_graph.query_snapshot() == graph_before
        assert runtime.owners.agent_runtime.query_snapshot() == agent_before
        assert runtime.feed.current_revision() == feed_before

        admission = runtime.owners.research_graph.admit_experiment(
            intent=intent,
            runtime_binding=runtime_a,
            definition_binding=definition_binding,
            implementation_binding=implementation_a,
            idempotency_key="implementation-binding-valid",
        )
        runtime_b = replace(
            runtime_a,
            runner_bundle_hash=hashlib.sha256(
                implementation_b_content
            ).hexdigest(),
        )
        agent_before = runtime.owners.agent_runtime.query_snapshot()
        feed_before = runtime.feed.current_revision()
        with pytest.raises(
            OwnerConflict,
            match="experiment_implementation_binding_mismatch",
        ):
            runtime.owners.agent_runtime.admit_experiment(
                admission=admission,
                runtime_binding=runtime_b,
            )
        assert runtime.owners.agent_runtime.query_snapshot() == agent_before
        assert runtime.feed.current_revision() == feed_before
    finally:
        runtime.close()


def test_result_reconciliation_pages_past_sixty_four_settled_runs(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path / "experiment-reconciliation-pagination")
    try:
        quest = _confirm_direct_quest(runtime)
        base = ExperimentIntent(
            execution_request_ref="pagination-0",
            quest_ref=quest["quest_ref"],
            title="bounded reconciliation pagination",
            hypothesis="已结算历史不得饿死后续 Formal Measurement。",
            variant_parameter=-0.25,
            sample_count=16,
        )
        for index in range(64):
            started = runtime.experiment.start(
                replace(base, execution_request_ref=f"pagination-{index}"),
                f"pagination-{index}",
            )
            assert runtime.experiment.process_once()
            assert runtime.experiment.process_once()
            assert runtime.experiment.process_once()
            settled = runtime.experiment.query(
                started["identities"]["evaluation_attempt_ref"]
            )
            assert settled["formal_measurement"]["status"] == "accepted"

        pending = runtime.experiment.start(
            replace(base, execution_request_ref="pagination-64"),
            "pagination-64",
        )
        pending_ref = pending["identities"]["evaluation_attempt_ref"]
        assert runtime.experiment.process_once()  # provider execution
        assert runtime.experiment.process_once()  # result assets after page one
        assert runtime.experiment.process_once()  # Formal Measurement
        accepted = runtime.experiment.query(pending_ref)
        assert accepted["formal_measurement"]["status"] == "accepted"
    finally:
        runtime.close()


def test_semantic_request_replay_uses_frozen_admission_after_provider_upgrade(
    tmp_path: Path,
) -> None:
    provider = _DeterministicExperimentProvider()
    runtime = _runtime(tmp_path / "experiment-frozen-replay", provider)
    try:
        quest = _confirm_direct_quest(runtime)
        intent = ExperimentIntent(
            execution_request_ref="frozen-replay-request",
            quest_ref=quest["quest_ref"],
            title="frozen technical replay",
            hypothesis="传输重试不得用新 provider revision 改写已接纳请求。",
            variant_parameter=-0.25,
            sample_count=16,
        )
        admitted = runtime.experiment.start(intent, "frozen-replay-transport")
        owners = runtime.owners
        accepted_snapshots = tuple(
            owner.query_snapshot()
            for owner in (
                owners.research_memory,
                owners.research_graph,
                owners.agent_runtime,
            )
        )
        accepted_feed_revision = runtime.feed.current_revision()
        provider_calls = (
            provider.runtime_binding_calls,
            provider.implementation_bundle_calls,
            provider.execute_calls,
        )

        provider._IMPLEMENTATION_BUNDLE = b"test-experiment-provider-v2"
        for transport_key in (
            "frozen-replay-transport",
            "frozen-replay-new-transport",
        ):
            replayed = runtime.experiment.start(intent, transport_key)
            assert replayed == admitted
            assert tuple(
                owner.query_snapshot()
                for owner in (
                    owners.research_memory,
                    owners.research_graph,
                    owners.agent_runtime,
                )
            ) == accepted_snapshots
            assert runtime.feed.current_revision() == accepted_feed_revision
            assert (
                provider.runtime_binding_calls,
                provider.implementation_bundle_calls,
                provider.execute_calls,
            ) == provider_calls

        with pytest.raises(
            OwnerConflict,
            match="experiment_execution_request_conflict",
        ):
            runtime.experiment.start(
                replace(intent, title="mutated after provider upgrade"),
                "frozen-replay-mutated-transport",
            )
        with pytest.raises(OwnerConflict, match="experiment_idempotency_conflict"):
            runtime.experiment.start(
                replace(
                    intent,
                    execution_request_ref="different-request-reusing-transport",
                ),
                "frozen-replay-new-transport",
            )
        assert tuple(
            owner.query_snapshot()
            for owner in (
                owners.research_memory,
                owners.research_graph,
                owners.agent_runtime,
            )
        ) == accepted_snapshots
        assert runtime.feed.current_revision() == accepted_feed_revision
        assert (
            provider.runtime_binding_calls,
            provider.implementation_bundle_calls,
            provider.execute_calls,
        ) == provider_calls
    finally:
        runtime.close()


@pytest.mark.parametrize(
    "source_state",
    ("admitted", "running", "failed", "executed_without_assets"),
)
def test_remeasurement_rejects_source_without_accepted_retrain_assets(
    tmp_path: Path,
    source_state: str,
) -> None:
    provider = _DeterministicExperimentProvider()
    runtime = _runtime(
        tmp_path / f"experiment-unaccepted-source-{source_state}",
        provider,
    )
    try:
        quest = _confirm_direct_quest(runtime)
        source = runtime.experiment.start(
            ExperimentIntent(
                execution_request_ref=f"unaccepted-source-{source_state}",
                quest_ref=quest["quest_ref"],
                title="unaccepted retrain source",
                hypothesis="未完成结果资产接纳的 retrain 不能成为 remeasure source。",
                variant_parameter=-0.25,
                sample_count=16,
            ),
            f"unaccepted-source-{source_state}",
        )
        if source_state != "admitted":
            run = runtime.owners.agent_runtime.claim_next_experiment()
            assert run is not None
            if source_state == "failed":
                runtime.owners.agent_runtime.fail_experiment_execution(
                    run_ref=run.run_ref,
                    attempt_ref=run.attempt_ref,
                    fence_ref=run.fence_ref,
                    failure_code="source_execution_failed",
                )
            elif source_state == "executed_without_assets":
                runtime.owners.agent_runtime.complete_experiment_execution(
                    run_ref=run.run_ref,
                    attempt_ref=run.attempt_ref,
                    fence_ref=run.fence_ref,
                    result=ExperimentProviderResult(
                        checkpoint_content=b'{"weights":[-0.25]}',
                        analysis={"direction": "negative"},
                        result_content={
                            "schema_ref": (
                                "meta-research/micro-experiment-result/v1"
                            ),
                            "metrics": {
                                "baseline_mean": 0.0,
                                "variant_mean": -0.25,
                                "mean_delta": -0.25,
                            },
                            "aggregation": (
                                "single fixed sample set; arithmetic mean"
                            ),
                        },
                        adapter_kind="unaccepted_source_fixture",
                    ).as_document(),
                )
            else:
                assert source_state == "running"

        owners = runtime.owners
        before = tuple(
            owner.query_snapshot()
            for owner in (
                owners.research_memory,
                owners.research_graph,
                owners.agent_runtime,
            )
        )
        feed_revision = runtime.feed.current_revision()
        provider_calls = (
            provider.runtime_binding_calls,
            provider.implementation_bundle_calls,
            provider.execute_calls,
        )
        with pytest.raises(
            OwnerConflict,
            match="experiment_source_variant_run_not_executed",
        ):
            runtime.experiment.start(
                ExperimentIntent(
                    execution_request_ref=f"remeasure-unaccepted-{source_state}",
                    quest_ref=quest["quest_ref"],
                    title="invalid remeasure source",
                    hypothesis="只有已接纳 retrain 资产的 source 才能复测。",
                    variant_parameter=-0.25,
                    sample_count=16,
                    request_kind="remeasure",
                    source_variant_run_ref=source["identities"][
                        "variant_run_ref"
                    ],
                ),
                f"remeasure-unaccepted-{source_state}",
            )
        assert tuple(
            owner.query_snapshot()
            for owner in (
                owners.research_memory,
                owners.research_graph,
                owners.agent_runtime,
            )
        ) == before
        assert runtime.feed.current_revision() == feed_revision
        assert (
            provider.runtime_binding_calls,
            provider.implementation_bundle_calls,
            provider.execute_calls,
        ) == provider_calls
    finally:
        runtime.close()


def test_remeasurement_cannot_graft_unreceipted_checkpoint_onto_source_variant(
    tmp_path: Path,
) -> None:
    provider = _DeterministicExperimentProvider()
    runtime = _runtime(tmp_path / "experiment-checkpoint-graft", provider)
    try:
        quest = _confirm_direct_quest(runtime)
        base = ExperimentIntent(
            execution_request_ref="checkpoint-graft-source",
            quest_ref=quest["quest_ref"],
            title="checkpoint graft source",
            hypothesis="只有 AR manifest 证明的 checkpoint 才能挂到 VariantRun。",
            variant_parameter=-0.25,
            sample_count=16,
        )
        source = runtime.experiment.start(base, "checkpoint-graft-source")
        assert _drain_experiments(runtime) == 3
        measured = runtime.experiment.start(
            replace(
                base,
                execution_request_ref="checkpoint-graft-remeasure",
                request_kind="remeasure",
                source_variant_run_ref=source["identities"]["variant_run_ref"],
            ),
            "checkpoint-graft-remeasure",
        )
        assert runtime.experiment.process_once()
        assert runtime.experiment.process_once()
        evaluation_attempt_ref = measured["identities"]["evaluation_attempt_ref"]
        run = runtime.owners.agent_runtime.query_experiment_run(
            evaluation_attempt_ref
        )
        assert run is not None
        assert run.result_hash is not None
        assert run.execution_receipt is not None
        accepted_roles = runtime.owners.research_graph.query_experiment_asset_roles(
            evaluation_attempt_ref
        )
        by_role = {
            role: tuple(item.binding for item in accepted_roles if item.role == role)
            for role in (
                "checkpoint_artifact",
                "log_asset",
                "analysis_asset",
                "result_content",
            )
        }
        forged_checkpoint = _accept_test_asset(
            runtime,
            name="checkpoint-graft-forged.json",
            content=b'{"weights":[999.0]}',
        )
        grafted_roles = {
            **by_role,
            "checkpoint_artifact": (
                *by_role["checkpoint_artifact"],
                forged_checkpoint,
            ),
        }

        with pytest.raises(TypeError, match="run_ref"):
            runtime.owners.research_graph.accept_experiment_asset_roles(
                evaluation_attempt_ref=evaluation_attempt_ref,
                roles=grafted_roles,
            )
        with pytest.raises(
            OwnerConflict,
            match="experiment_asset_execution_component_mismatch",
        ):
            runtime.owners.research_graph.accept_experiment_asset_roles(
                evaluation_attempt_ref=evaluation_attempt_ref,
                roles=grafted_roles,
                run_ref=run.run_ref,
                execution_attempt_ref=run.attempt_ref,
                fence_ref=run.fence_ref,
                execution_result_hash=run.result_hash,
                execution_receipt=run.execution_receipt,
            )
        assert all(
            role.binding.version_ref != forged_checkpoint.version_ref
            for role in runtime.owners.research_graph.query_experiment_asset_roles(
                evaluation_attempt_ref
            )
        )

        owners = runtime.owners
        before = tuple(
            owner.query_snapshot()
            for owner in (
                owners.research_memory,
                owners.research_graph,
                owners.agent_runtime,
            )
        )
        feed_revision = runtime.feed.current_revision()
        provider_calls = (
            provider.runtime_binding_calls,
            provider.implementation_bundle_calls,
            provider.execute_calls,
        )
        with pytest.raises(
            OwnerConflict,
            match="experiment_checkpoint_selection_not_found",
        ):
            runtime.experiment.start(
                replace(
                    base,
                    execution_request_ref="checkpoint-graft-followup",
                    request_kind="remeasure",
                    source_variant_run_ref=source["identities"]["variant_run_ref"],
                    selected_checkpoint_role_refs=(
                        forged_checkpoint.version_ref,
                    ),
                ),
                "checkpoint-graft-followup",
            )
        assert tuple(
            owner.query_snapshot()
            for owner in (
                owners.research_memory,
                owners.research_graph,
                owners.agent_runtime,
            )
        ) == before
        assert runtime.feed.current_revision() == feed_revision
        assert (
            provider.runtime_binding_calls,
            provider.implementation_bundle_calls,
            provider.execute_calls,
        ) == provider_calls
    finally:
        runtime.close()


def test_missing_provider_implementation_bundle_fails_closed_without_side_effects(
    tmp_path: Path,
) -> None:
    provider = _DeclaredOnlyExperimentProvider()
    runtime = _runtime(tmp_path / "experiment-missing-bundle", provider)
    try:
        quest = _confirm_direct_quest(runtime)
        owners = runtime.owners
        before = tuple(
            owner.query_snapshot()
            for owner in (
                owners.research_memory,
                owners.research_graph,
                owners.agent_runtime,
            )
        )
        feed_revision = runtime.feed.current_revision()
        with pytest.raises(
            OwnerConflict, match="experiment_implementation_bundle_unavailable"
        ):
            runtime.experiment.start(
                ExperimentIntent(
                    execution_request_ref="missing-implementation-bundle",
                    quest_ref=quest["quest_ref"],
                    title="missing implementation bundle",
                    hypothesis="声明不能替代实际 implementation bytes。",
                    variant_parameter=-0.25,
                    sample_count=16,
                ),
                "missing-implementation-bundle",
            )
        assert provider.execute_calls == 0
        assert runtime.feed.current_revision() == feed_revision
        assert tuple(
            owner.query_snapshot()
            for owner in (
                owners.research_memory,
                owners.research_graph,
                owners.agent_runtime,
            )
        ) == before
    finally:
        runtime.close()


def test_retrain_and_remeasure_are_explicit_semantic_requests(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path / "experiment-request-kinds")
    try:
        quest = _confirm_direct_quest(runtime)
        initial = ExperimentIntent(
            execution_request_ref="semantic-kind-initial",
            quest_ref=quest["quest_ref"],
            title="显式 retrain",
            hypothesis="技术重放不等于新的领域身份。",
            variant_parameter=-0.25,
            sample_count=16,
            request_kind="retrain",
        )
        admitted = runtime.experiment.start(initial, "semantic-kind-initial")

        replay = runtime.experiment.start(initial, "semantic-kind-initial-replay")
        assert replay["identities"] == admitted["identities"]
        assert replay["execution"]["run_ref"] == admitted["execution"]["run_ref"]
        assert _drain_experiments(runtime) == 3

        remeasured = runtime.experiment.start(
            replace(
                initial,
                execution_request_ref="semantic-kind-remeasure",
                request_kind="remeasure",
                source_variant_run_ref=admitted["identities"]["variant_run_ref"],
            ),
            "semantic-kind-remeasure",
        )
        for reused in (
            "baseline_ref",
            "variant_ref",
            "evaluation_protocol_ref",
            "protocol_version_ref",
            "evaluation_ref",
            "variant_run_ref",
        ):
            assert remeasured["identities"][reused] == admitted["identities"][reused]
        assert (
            remeasured["identities"]["evaluation_attempt_ref"]
            != admitted["identities"]["evaluation_attempt_ref"]
        )
        assert (
            remeasured["frozen_inputs"]["variant_run"]["binding_ref"]
            == admitted["frozen_inputs"]["variant_run"]["binding_ref"]
        )
        assert (
            remeasured["frozen_inputs"]["evaluation_attempt"]["binding_ref"]
            != admitted["frozen_inputs"]["evaluation_attempt"]["binding_ref"]
        )

        retrained = runtime.experiment.start(
            replace(
                initial,
                execution_request_ref="semantic-kind-retrain",
            ),
            "semantic-kind-retrain",
        )
        assert (
            retrained["identities"]["variant_run_ref"]
            != admitted["identities"]["variant_run_ref"]
        )
        assert (
            retrained["identities"]["evaluation_attempt_ref"]
            != admitted["identities"]["evaluation_attempt_ref"]
        )

        with pytest.raises(OwnerConflict, match="experiment_source_variant_run_required"):
            runtime.experiment.start(
                replace(
                    initial,
                    execution_request_ref="semantic-kind-invalid-remeasure",
                    request_kind="remeasure",
                ),
                "semantic-kind-invalid-remeasure",
            )
    finally:
        runtime.close()


def test_remeasurement_selects_zero_one_or_many_source_checkpoints(
    tmp_path: Path,
) -> None:
    provider = _MultipleCheckpointProvider()
    runtime = _runtime(
        tmp_path / "experiment-checkpoint-selection",
        provider,
    )
    try:
        quest = _confirm_direct_quest(runtime)
        base = ExperimentIntent(
            execution_request_ref="checkpoint-source",
            quest_ref=quest["quest_ref"],
            title="checkpoint source",
            hypothesis="同一 VariantRun 的 checkpoint 可被后续测量显式选择。",
            variant_parameter=-0.25,
            sample_count=16,
        )
        source = runtime.experiment.start(base, "checkpoint-source")
        assert runtime.experiment.process_once()
        assert runtime.experiment.process_once()
        assert runtime.experiment.process_once()
        source = runtime.experiment.query(
            source["identities"]["evaluation_attempt_ref"]
        )
        checkpoint_refs = tuple(
            role["role_ref"] for role in source["assets"]["checkpoint_artifacts"]
        )
        assert len(checkpoint_refs) == 3
        checkpoint_roles = {
            role["role_ref"]: role
            for role in source["assets"]["checkpoint_artifacts"]
        }

        foreign = runtime.experiment.start(
            replace(base, execution_request_ref="checkpoint-foreign"),
            "checkpoint-foreign",
        )
        assert runtime.experiment.process_once()
        assert runtime.experiment.process_once()
        assert runtime.experiment.process_once()
        foreign = runtime.experiment.query(
            foreign["identities"]["evaluation_attempt_ref"]
        )
        foreign_checkpoint = foreign["assets"]["checkpoint_artifacts"][0][
            "role_ref"
        ]

        observed_results = []
        for suffix, selection, expected_variant_mean in (
            ("zero", (), -0.25),
            ("one", checkpoint_refs[1:2], -0.125),
            (
                "many",
                tuple(reversed(checkpoint_refs)),
                (-0.25 - 0.125 - 0.0625) / 3,
            ),
        ):
            measured = runtime.experiment.start(
                replace(
                    base,
                    execution_request_ref=f"checkpoint-remeasure-{suffix}",
                    request_kind="remeasure",
                    source_variant_run_ref=source["identities"]["variant_run_ref"],
                    selected_checkpoint_role_refs=selection,
                ),
                f"checkpoint-remeasure-{suffix}",
            )
            assert measured["identities"]["variant_run_ref"] == source[
                "identities"
            ]["variant_run_ref"]
            assert tuple(
                measured["intent"]["selected_checkpoint_role_refs"]
            ) == selection
            assert _drain_experiments(runtime) == 3
            result = runtime.experiment.query(
                measured["identities"]["evaluation_attempt_ref"]
            )
            assert tuple(
                role["role_ref"]
                for role in result["assets"]["checkpoint_artifacts"]
            ) == checkpoint_refs
            run = runtime.owners.agent_runtime.query_experiment_run(
                measured["identities"]["evaluation_attempt_ref"]
            )
            assert run is not None
            assert run.result is not None
            assert run.result["checkpoint_content_base64"] is None
            assert run.result_hash is not None
            assert run.execution_receipt is not None
            manifest = (
                runtime.owners.agent_runtime.verify_experiment_execution_receipt(
                    run_ref=run.run_ref,
                    attempt_ref=run.attempt_ref,
                    fence_ref=run.fence_ref,
                    evaluation_attempt_ref=run.evaluation_attempt_ref,
                    result_hash=run.result_hash,
                    receipt=run.execution_receipt,
                )
            )
            assert manifest.checkpoint_content_hashes == ()
            result = result["formal_measurement"]["metric_result"]
            assert result["metrics"]["variant_mean"] == pytest.approx(
                expected_variant_mean
            )
            observed_results.append(result["metrics"]["variant_mean"])

            request = provider.requests[-1]
            assert request.request_kind == "remeasure"
            assert tuple(
                checkpoint.role_ref for checkpoint in request.selected_checkpoints
            ) == selection
            assert tuple(
                checkpoint.ordinal for checkpoint in request.selected_checkpoints
            ) == tuple(range(len(selection)))
            for checkpoint in request.selected_checkpoints:
                role = checkpoint_roles[checkpoint.role_ref]
                materialized = runtime.owners.research_memory.materialize_asset(
                    role["version_ref"]
                )
                assert checkpoint.version_ref == role["version_ref"]
                assert checkpoint.content_hash == role["content_hash"]
                assert checkpoint.content == materialized.content

        assert len(set(observed_results)) == 3

        with pytest.raises(
            OwnerConflict, match="experiment_checkpoint_selection_foreign"
        ):
            runtime.experiment.start(
                replace(
                    base,
                    execution_request_ref="checkpoint-remeasure-foreign",
                    request_kind="remeasure",
                    source_variant_run_ref=source["identities"]["variant_run_ref"],
                    selected_checkpoint_role_refs=(foreign_checkpoint,),
                ),
                "checkpoint-remeasure-foreign",
            )
    finally:
        runtime.close()


def test_daemon_recovery_replaces_only_execution_attempt_and_fences_late_writes(
    tmp_path: Path,
) -> None:
    root = tmp_path / "experiment-daemon-recovery"
    runtime = _runtime(root)
    quest = _confirm_direct_quest(runtime)
    admitted = runtime.experiment.start(
        ExperimentIntent(
            execution_request_ref="daemon-recovery-request",
            quest_ref=quest["quest_ref"],
            title="daemon recovery",
            hypothesis="技术恢复只更换 AR Attempt/Session/Fence。",
            variant_parameter=-0.25,
            sample_count=16,
        ),
        "daemon-recovery-request",
    )
    domain_identities = admitted["identities"]
    running = runtime.owners.agent_runtime.claim_next_experiment()
    assert running is not None
    old_run_ref = running.run_ref
    old_attempt_ref = running.attempt_ref
    old_fence_ref = running.fence_ref
    runtime.owners.agent_runtime.record_experiment_observation(
        run_ref=old_run_ref,
        attempt_ref=old_attempt_ref,
        fence_ref=old_fence_ref,
        kind="stdout",
        payload={"line": "retired-attempt-output", "stream": "stdout"},
        observed_at=1_720_000_008.0,
    )
    runtime.close()

    recovered_runtime = _runtime(root)
    try:
        recovered = recovered_runtime.experiment.query(
            domain_identities["evaluation_attempt_ref"]
        )
        execution = recovered["execution"]
        assert recovered["identities"] == domain_identities
        assert execution["run_ref"] == old_run_ref
        assert execution["attempt_generation"] == 2
        assert execution["attempt_ref"] != old_attempt_ref
        assert execution["fence_ref"] != old_fence_ref
        assert execution["stdout_observation"] == {
            "mode": "raw_stdout",
            "complete": False,
            "total": 0,
            "count": 0,
            "truncated": False,
            "dropped": 0,
            "first_sequence": None,
            "last_sequence": None,
            "observed_at": None,
        }

        with pytest.raises(OwnerConflict, match="experiment_fence_stale"):
            recovered_runtime.owners.agent_runtime.record_experiment_observation(
                run_ref=old_run_ref,
                attempt_ref=old_attempt_ref,
                fence_ref=old_fence_ref,
                kind="stdout",
                payload={"line": "late", "stream": "stdout"},
                observed_at=1_720_000_009.0,
            )
        with pytest.raises(OwnerConflict, match="experiment_fence_stale"):
            recovered_runtime.owners.agent_runtime.complete_experiment_execution(
                run_ref=old_run_ref,
                attempt_ref=old_attempt_ref,
                fence_ref=old_fence_ref,
                result=_DeterministicExperimentProvider()
                .execute(
                    ExperimentProviderRequest(
                        identities=recovered_runtime.owners.research_graph.query_experiment(
                            domain_identities["evaluation_attempt_ref"]
                        ).identities,
                        variant_run_binding=recovered_runtime.owners.research_graph.query_experiment(
                            domain_identities["evaluation_attempt_ref"]
                        ).variant_run_binding,
                        evaluation_attempt_binding=recovered_runtime.owners.research_graph.query_experiment(
                            domain_identities["evaluation_attempt_ref"]
                        ).evaluation_attempt_binding,
                        required_metrics=(
                            "baseline_mean",
                            "variant_mean",
                            "mean_delta",
                        ),
                    ),
                    lambda _observation: None,
                )
                .as_document(),
            )
        assert recovered_runtime.experiment.process_once()
        executed = recovered_runtime.experiment.query(
            domain_identities["evaluation_attempt_ref"]
        )
        assert executed["execution"]["status"] == "executed"
        assert executed["execution"]["stdout_observation"]["total"] == 1
        assert executed["identities"] == domain_identities
    finally:
        recovered_runtime.close()


def test_worker_reconciles_domain_admission_after_pre_runtime_crash(
    tmp_path: Path,
) -> None:
    root = tmp_path / "experiment-admission-reconciliation"
    provider = _DeterministicExperimentProvider()
    runtime = _runtime(root, provider)
    quest = _confirm_direct_quest(runtime)
    crashing_service = ExperimentService(
        runtime.owners.research_graph,
        _CrashBeforeRuntimeAdmission(),
        runtime.owners.research_memory,
        provider,
    )
    with pytest.raises(
        RuntimeError, match="simulated_crash_before_runtime_admission"
    ):
        crashing_service.start(
            ExperimentIntent(
                execution_request_ref="admission-reconciliation-request",
                quest_ref=quest["quest_ref"],
                title="recover RG admission",
                hypothesis="RG accepted 后的崩溃不得让实验永久停在 not_attempted。",
                variant_parameter=-0.25,
                sample_count=16,
            ),
            "admission-reconciliation-request",
        )
    pending = runtime.experiment.query_current()
    assert pending is not None
    assert pending["execution"] == {"status": "not_attempted"}
    frozen_identities = pending["identities"]
    frozen_request = pending["execution_request"]
    memory_snapshot = runtime.owners.research_memory.query_snapshot()
    graph_snapshot = runtime.owners.research_graph.query_snapshot()
    runtime_snapshot = runtime.owners.agent_runtime.query_snapshot()
    provider_runtime_calls = provider.runtime_binding_calls
    provider_execute_calls = provider.execute_calls
    runtime.close()

    recovered_runtime = _runtime(root, provider)
    try:
        assert recovered_runtime.experiment.process_once()
        reconciled = recovered_runtime.experiment.query_current()
        assert reconciled is not None
        assert reconciled["identities"] == frozen_identities
        assert reconciled["execution_request"] == frozen_request
        assert reconciled["execution"]["status"] == "admitted"
        assert reconciled["execution"]["attempt_generation"] == 1
        assert provider.runtime_binding_calls == provider_runtime_calls
        assert provider.execute_calls == provider_execute_calls
        assert (
            recovered_runtime.owners.research_memory.query_snapshot()
            == memory_snapshot
        )
        assert recovered_runtime.owners.research_graph.query_snapshot() == graph_snapshot
        reconciled_runtime_snapshot = (
            recovered_runtime.owners.agent_runtime.query_snapshot()
        )
        assert reconciled_runtime_snapshot.facts["experiment_run_count"] == (
            runtime_snapshot.facts["experiment_run_count"] + 1
        )
    finally:
        recovered_runtime.close()


def test_worker_restart_does_not_replay_historical_bundle_target_admission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "bundle-target-admission-reconciliation"
    provider = _DeterministicExperimentProvider()
    runtime = _runtime(root, provider)
    evaluation_attempt_ref = ""
    try:
        quest = _confirm_direct_quest(runtime)
        intent = ExperimentIntent(
            execution_request_ref="bundle-target-historical-row",
            quest_ref=quest["quest_ref"],
            title="historical Target Experiment",
            hypothesis="Historical Target-linked Experiment rows are diagnostic only.",
            variant_parameter=-0.25,
            sample_count=16,
        )
        runtime_binding = provider.runtime_binding()
        definition_binding = _accept_test_asset(
            runtime,
            name="historical-bundle-target-definition.json",
            content=canonical_json(
                experiment_definition_document(intent, runtime_binding)
            ).encode("utf-8"),
        )
        implementation_binding = _accept_test_asset(
            runtime,
            name="historical-bundle-target-implementation.json",
            content=provider.implementation_bundle(),
        )
        # Simulate an internally valid row accepted by a pre-guard binary.
        # Production admission remains forbidden; only the historical reader
        # and restart filter are under test here.
        with monkeypatch.context() as legacy_seed:
            legacy_seed.setattr(
                "meta_research.owners.research_graph."
                "_forbid_bundle_target_experiment_write",
                lambda _execution_request_ref: None,
            )
            admission = runtime.owners.research_graph.admit_experiment(
                intent=intent,
                runtime_binding=runtime_binding,
                definition_binding=definition_binding,
                implementation_binding=implementation_binding,
                idempotency_key="historical-bundle-target-rg-admission",
            )
        evaluation_attempt_ref = admission.identities.evaluation_attempt_ref
        assert (
            runtime.owners.agent_runtime.query_experiment_run(evaluation_attempt_ref)
            is None
        )
    finally:
        runtime.close()

    recovered_runtime = _runtime(root, provider)
    try:
        before_counts = _experiment_write_counts(recovered_runtime)
        before_snapshots = tuple(
            owner.query_snapshot()
            for owner in (
                recovered_runtime.owners.research_memory,
                recovered_runtime.owners.research_graph,
                recovered_runtime.owners.agent_runtime,
            )
        )
        before_provider_calls = (
            provider.runtime_binding_calls,
            provider.implementation_bundle_calls,
            provider.execute_calls,
        )

        assert recovered_runtime.experiment.process_once() is False

        assert (
            recovered_runtime.owners.agent_runtime.query_experiment_run(
                evaluation_attempt_ref
            )
            is None
        )
        assert (
            provider.runtime_binding_calls,
            provider.implementation_bundle_calls,
            provider.execute_calls,
        ) == before_provider_calls
        assert _experiment_write_counts(recovered_runtime) == before_counts
        assert tuple(
            owner.query_snapshot()
            for owner in (
                recovered_runtime.owners.research_memory,
                recovered_runtime.owners.research_graph,
                recovered_runtime.owners.agent_runtime,
            )
        ) == before_snapshots
    finally:
        recovered_runtime.close()


def test_running_observations_publish_feed_and_support_bounded_event_cursors(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path / "experiment-event-cursor")
    try:
        quest = _confirm_direct_quest(runtime)
        admitted = runtime.experiment.start(
            ExperimentIntent(
                execution_request_ref="event-cursor-request",
                quest_ref=quest["quest_ref"],
                title="event cursor",
                hypothesis="运行中的可观察事件必须立即进入 durable feed。",
                variant_parameter=-0.25,
                sample_count=16,
            ),
            "event-cursor-request",
        )
        running = runtime.owners.agent_runtime.claim_next_experiment()
        assert running is not None
        feed_revision = runtime.feed.current_revision()
        owner_revision = runtime.owners.agent_runtime.query_snapshot().revision
        for index in range(5):
            runtime.owners.agent_runtime.record_experiment_observation(
                run_ref=running.run_ref,
                attempt_ref=running.attempt_ref,
                fence_ref=running.fence_ref,
                kind="stdout",
                payload={"line": f"line-{index}", "stream": "stdout"},
                observed_at=1_720_000_100.0 + index,
            )

        page = runtime.feed.read_after(feed_revision, limit=10)
        assert [event.event_type for event in page.events] == [
            "agent_runtime.experiment_observed"
        ] * 5
        assert runtime.owners.agent_runtime.query_snapshot().revision == (
            owner_revision + 5
        )
        running_public = runtime.experiment.query(
            admitted["identities"]["evaluation_attempt_ref"]
        )["execution"]
        assert running_public["status"] == "running"
        assert running_public["stdout_observation"] == {
            "mode": "raw_stdout",
            "complete": False,
            "total": 5,
            "count": 5,
            "truncated": False,
            "dropped": 0,
            "first_sequence": 2,
            "last_sequence": 6,
            "observed_at": 1_720_000_104.0,
        }

        events = runtime.experiment.query_events(
            admitted["identities"]["evaluation_attempt_ref"],
            after_sequence=2,
            limit=2,
        )
        assert [event["sequence"] for event in events] == [3, 4]
        assert [event["payload"]["line"] for event in events] == [
            "line-1",
            "line-2",
        ]
        with pytest.raises(OwnerConflict, match="experiment_event_cursor_invalid"):
            runtime.experiment.query_events(
                admitted["identities"]["evaluation_attempt_ref"],
                after_sequence=-1,
                limit=2,
            )
        with pytest.raises(OwnerConflict, match="experiment_event_limit_invalid"):
            runtime.experiment.query_events(
                admitted["identities"]["evaluation_attempt_ref"],
                after_sequence=0,
                limit=513,
            )
    finally:
        runtime.close()


def test_stdout_tail_metadata_and_log_asset_use_exact_raw_stdout_counts(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path / "experiment-stdout-tail")
    try:
        quest = _confirm_direct_quest(runtime)
        admitted = runtime.experiment.start(
            ExperimentIntent(
                execution_request_ref="stdout-tail-request",
                quest_ref=quest["quest_ref"],
                title="stdout bounded tail",
                hypothesis="bounded projection 不得把 telemetry 当成丢失 stdout。",
                variant_parameter=0.0,
                sample_count=16,
            ),
            "stdout-tail-request",
        )
        running = runtime.owners.agent_runtime.claim_next_experiment()
        assert running is not None
        for index in range(150):
            runtime.owners.agent_runtime.record_experiment_observation(
                run_ref=running.run_ref,
                attempt_ref=running.attempt_ref,
                fence_ref=running.fence_ref,
                kind="stdout",
                payload={"line": f"raw-{index}", "stream": "stdout"},
                observed_at=1_720_001_000.0 + index,
            )
            runtime.owners.agent_runtime.record_experiment_observation(
                run_ref=running.run_ref,
                attempt_ref=running.attempt_ref,
                fence_ref=running.fence_ref,
                kind="telemetry",
                payload={"sample": index},
                observed_at=1_720_001_000.5 + index,
            )

        projected = runtime.experiment.query(
            admitted["identities"]["evaluation_attempt_ref"]
        )["execution"]
        assert len(projected["events"]) == 256
        assert projected["stdout_observation"] == {
            "mode": "raw_stdout",
            "complete": False,
            "total": 150,
            "count": 128,
            "truncated": True,
            "dropped": 22,
            "first_sequence": 46,
            "last_sequence": 300,
            "observed_at": 1_720_001_149.0,
        }

        all_events = runtime.experiment.query_events(
            admitted["identities"]["evaluation_attempt_ref"],
            after_sequence=0,
            limit=512,
        )
        assert len(all_events) == 301
        assert len([event for event in all_events if event["kind"] == "stdout"]) == 150

        runtime.owners.agent_runtime.complete_experiment_execution(
            run_ref=running.run_ref,
            attempt_ref=running.attempt_ref,
            fence_ref=running.fence_ref,
            result=ExperimentProviderResult(
                checkpoint_content=b'{"weights":[0.0]}',
                analysis={"direction": "zero"},
                result_content={
                    "schema_ref": "meta-research/micro-experiment-result/v1",
                    "metrics": {
                        "baseline_mean": 0.0,
                        "variant_mean": 0.0,
                        "mean_delta": 0.0,
                    },
                    "aggregation": "fixed sample arithmetic mean",
                },
                adapter_kind="test_manual_completion",
            ).as_document(),
        )
        assert runtime.experiment.process_once()
        assets = runtime.experiment.query(
            admitted["identities"]["evaluation_attempt_ref"]
        )["assets"]
        log_role = assets["log_assets"][0]
        materialized = runtime.owners.research_memory.materialize_asset(
            log_role["version_ref"]
        )
        log_document = json.loads(materialized.content.decode("utf-8"))
        assert len(log_document["stdout"]) == 150
        assert log_document["stdout"][0]["sequence"] == 2
        assert log_document["stdout"][-1]["sequence"] == 300
        assert log_document["observation"] == {
            "mode": "raw_stdout",
            "complete": True,
            "truncated": False,
            "dropped": 0,
            "event_count": 302,
            "stdout_count": 150,
        }
    finally:
        runtime.close()


def test_asset_acceptance_rejects_rm_assets_unrelated_to_ar_result_components(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path / "experiment-formal-component-forgery")
    try:
        quest = _confirm_direct_quest(runtime)
        admitted = runtime.experiment.start(
            ExperimentIntent(
                execution_request_ref="formal-component-forgery-request",
                quest_ref=quest["quest_ref"],
                title="forged formal components",
                hypothesis="真实执行收据不能替无关 RM 结果内容背书。",
                variant_parameter=-0.25,
                sample_count=16,
            ),
            "formal-component-forgery-request",
        )
        assert runtime.experiment.process_once()
        evaluation_attempt_ref = admitted["identities"][
            "evaluation_attempt_ref"
        ]
        executed = runtime.owners.agent_runtime.query_experiment_run(
            evaluation_attempt_ref
        )
        assert executed is not None
        assert executed.result_hash is not None
        assert executed.execution_receipt is not None

        forged_result = {
            "schema_ref": "meta-research/micro-experiment-result/v1",
            "metrics": {
                "baseline_mean": 100.0,
                "variant_mean": 200.0,
                "mean_delta": 100.0,
            },
            "aggregation": "unrelated accepted RM content",
        }
        forged_roles = {
            "checkpoint_artifact": (
                _accept_test_asset(
                    runtime,
                    name="forged-checkpoint.json",
                    content=b'{"weights":[999.0]}',
                ),
            ),
            "log_asset": (
                _accept_test_asset(
                    runtime,
                    name="forged-log.json",
                    content=b'{"stdout":[]}',
                ),
            ),
            "analysis_asset": (
                _accept_test_asset(
                    runtime,
                    name="forged-analysis.json",
                    content=b'{"direction":"forged"}',
                ),
            ),
            "result_content": (
                _accept_test_asset(
                    runtime,
                    name="forged-result.json",
                    content=canonical_json(forged_result).encode("utf-8"),
                ),
            ),
        }
        with pytest.raises(
            OwnerConflict,
            match="experiment_asset_execution_component_mismatch",
        ):
            runtime.owners.research_graph.accept_experiment_asset_roles(
                evaluation_attempt_ref=evaluation_attempt_ref,
                roles=forged_roles,
                run_ref=executed.run_ref,
                execution_attempt_ref=executed.attempt_ref,
                fence_ref=executed.fence_ref,
                execution_result_hash=executed.result_hash,
                execution_receipt=executed.execution_receipt,
            )
        assert (
            runtime.owners.research_graph.query_experiment_asset_roles(
                evaluation_attempt_ref
            )
            == ()
        )
        assert (
            runtime.owners.research_graph.query_formal_metric_result(
                evaluation_attempt_ref
            )
            is None
        )
        with pytest.raises(
            OwnerConflict,
            match="experiment_source_variant_run_not_executed",
        ):
            runtime.experiment.start(
                ExperimentIntent(
                    execution_request_ref="forged-source-remeasure-request",
                    quest_ref=quest["quest_ref"],
                    title="forged source remeasure",
                    hypothesis="无关 checkpoint 不能证明 source 已执行。",
                    variant_parameter=-0.25,
                    sample_count=16,
                    request_kind="remeasure",
                    source_variant_run_ref=admitted["identities"][
                        "variant_run_ref"
                    ],
                ),
                "forged-source-remeasure-request",
            )
    finally:
        runtime.close()


def test_asset_acceptance_rejects_retrain_without_checkpoint_at_owner_boundary(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path / "experiment-formal-missing-checkpoint")
    try:
        quest = _confirm_direct_quest(runtime)
        admitted = runtime.experiment.start(
            ExperimentIntent(
                execution_request_ref="formal-missing-checkpoint-request",
                quest_ref=quest["quest_ref"],
                title="missing retrain checkpoint",
                hypothesis="retrain 的执行收据不能为零 checkpoint 的正式测量背书。",
                variant_parameter=-0.25,
                sample_count=16,
            ),
            "formal-missing-checkpoint-request",
        )
        evaluation_attempt_ref = admitted["identities"][
            "evaluation_attempt_ref"
        ]
        running = runtime.owners.agent_runtime.claim_next_experiment()
        assert running is not None
        analysis = {"direction": "negative"}
        result_content = {
            "schema_ref": "meta-research/micro-experiment-result/v1",
            "metrics": {
                "baseline_mean": 0.0,
                "variant_mean": -0.25,
                "mean_delta": -0.25,
            },
            "aggregation": "single fixed sample set; arithmetic mean",
        }
        completed = runtime.owners.agent_runtime.complete_experiment_execution(
            run_ref=running.run_ref,
            attempt_ref=running.attempt_ref,
            fence_ref=running.fence_ref,
            result=ExperimentProviderResult(
                checkpoint_content=None,
                analysis=analysis,
                result_content=result_content,
                adapter_kind="direct_owner_attack_fixture",
            ).as_document(),
        )
        assert completed.result_hash is not None
        assert completed.execution_receipt is not None
        events = runtime.experiment.query_events(
            evaluation_attempt_ref,
            after_sequence=0,
            limit=512,
        )
        with pytest.raises(
            OwnerConflict,
            match="experiment_asset_execution_component_mismatch",
        ):
            runtime.owners.research_graph.accept_experiment_asset_roles(
                evaluation_attempt_ref=evaluation_attempt_ref,
                roles={
                    "checkpoint_artifact": (),
                    "log_asset": (
                        _accept_test_asset(
                            runtime,
                            name="missing-checkpoint-log.json",
                            content=canonical_json(
                                experiment_execution_log_document(events)
                            ).encode("utf-8"),
                        ),
                    ),
                    "analysis_asset": (
                        _accept_test_asset(
                            runtime,
                            name="missing-checkpoint-analysis.json",
                            content=canonical_json(analysis).encode("utf-8"),
                        ),
                    ),
                    "result_content": (
                        _accept_test_asset(
                            runtime,
                            name="missing-checkpoint-result.json",
                            content=canonical_json(result_content).encode("utf-8"),
                        ),
                    ),
                },
                run_ref=completed.run_ref,
                execution_attempt_ref=completed.attempt_ref,
                fence_ref=completed.fence_ref,
                execution_result_hash=completed.result_hash,
                execution_receipt=completed.execution_receipt,
            )
        assert (
            runtime.owners.research_graph.query_experiment_asset_roles(
                evaluation_attempt_ref
            )
            == ()
        )
        assert (
            runtime.owners.research_graph.query_formal_metric_result(
                evaluation_attempt_ref
            )
            is None
        )
        before_remeasure = tuple(
            owner.query_snapshot()
            for owner in (
                runtime.owners.research_memory,
                runtime.owners.research_graph,
                runtime.owners.agent_runtime,
            )
        )
        before_feed = runtime.feed.current_revision()
        with pytest.raises(
            OwnerConflict,
            match="experiment_source_variant_run_not_executed",
        ):
            runtime.experiment.start(
                ExperimentIntent(
                    execution_request_ref=(
                        "formal-missing-checkpoint-remeasure-request"
                    ),
                    quest_ref=quest["quest_ref"],
                    title="remeasure missing checkpoint source",
                    hypothesis="零 checkpoint 的伪 retrain 不能成为复测来源。",
                    variant_parameter=-0.25,
                    sample_count=16,
                    request_kind="remeasure",
                    source_variant_run_ref=admitted["identities"][
                        "variant_run_ref"
                    ],
                ),
                "formal-missing-checkpoint-remeasure-request",
            )
        assert tuple(
            owner.query_snapshot()
            for owner in (
                runtime.owners.research_memory,
                runtime.owners.research_graph,
                runtime.owners.agent_runtime,
            )
        ) == before_remeasure
        assert runtime.feed.current_revision() == before_feed
    finally:
        runtime.close()


def test_incomplete_formal_measurement_is_durably_rejected_and_converges(
    tmp_path: Path,
) -> None:
    runtime = _runtime(
        tmp_path / "experiment-formal-rejected",
        _MetricsExperimentProvider(
            {"baseline_mean": 0.0, "variant_mean": -0.25}
        ),
    )
    try:
        quest = _confirm_direct_quest(runtime)
        admitted = runtime.experiment.start(
            ExperimentIntent(
                execution_request_ref="formal-rejected-request",
                quest_ref=quest["quest_ref"],
                title="partial metrics",
                hypothesis="缺少一个必需 Metric 的 Attempt 不能形成 MetricResult。",
                variant_parameter=-0.25,
                sample_count=16,
            ),
            "formal-rejected-request",
        )
        assert _drain_experiments(runtime) == 3
        rejected = runtime.experiment.query(
            admitted["identities"]["evaluation_attempt_ref"]
        )
        assert rejected["execution"]["status"] == "executed"
        assert rejected["assets"]["status"] == "accepted"
        assert rejected["formal_measurement"] == {
            "status": "rejected",
            "reason": {"code": "formal_measurement_metrics_incomplete"},
            "metric_result": None,
        }
        assert runtime.experiment.process_once() is False
    finally:
        runtime.close()


def _agent_runtime_experiment_storage(
    runtime,
) -> dict[str, tuple[tuple[object, ...], ...]]:
    tables = (
        "agent_runtime_state",
        "ar_experiment_runs",
        "ar_experiment_sessions",
        "ar_experiment_attempts",
        "ar_experiment_events",
        "ar_provider_units",
        "ar_run_controls",
        "ar_fence_revocations",
        "durable_feed",
        "projection_offsets",
    )
    with runtime._database.read() as connection:
        return {
            table: tuple(
                tuple(row)
                for row in connection.execute(
                    text(f"SELECT * FROM {table} ORDER BY rowid")
                ).all()
            )
            for table in tables
        }


def test_agent_runtime_rejects_direct_bundle_target_experiment_admission(
    tmp_path: Path,
) -> None:
    provider = _DeterministicExperimentProvider()
    runtime = _runtime(tmp_path / "agent-runtime-direct-target-admission", provider)
    try:
        quest = _confirm_direct_quest(runtime)
        intent = ExperimentIntent(
            execution_request_ref="standalone-request-before-forgery",
            quest_ref=quest["quest_ref"],
            title="standalone admission forged at AR boundary",
            hypothesis="AR must independently reject the formal Target namespace.",
            variant_parameter=-0.25,
            sample_count=16,
        )
        runtime_binding = provider.runtime_binding()
        definition_binding = _accept_test_asset(
            runtime,
            name="ar-direct-admission-definition.json",
            content=canonical_json(
                experiment_definition_document(intent, runtime_binding)
            ).encode("utf-8"),
        )
        implementation_binding = _accept_test_asset(
            runtime,
            name="ar-direct-admission-implementation.json",
            content=provider.implementation_bundle(),
        )
        admission = runtime.owners.research_graph.admit_experiment(
            intent=intent,
            runtime_binding=runtime_binding,
            definition_binding=definition_binding,
            implementation_binding=implementation_binding,
            idempotency_key="ar-direct-admission-domain",
        )
        forged_admission = replace(
            admission,
            execution_request=replace(
                admission.execution_request,
                execution_request_ref="bundle-target-forged-direct-ar-admission",
            ),
        )
        before = _agent_runtime_experiment_storage(runtime)

        with pytest.raises(
            OwnerConflict,
            match="bundle_target_experiment_write_forbidden",
        ):
            runtime.owners.agent_runtime.admit_experiment(
                admission=forged_admission,
                runtime_binding=runtime_binding,
            )

        assert _agent_runtime_experiment_storage(runtime) == before
        assert (
            runtime.owners.agent_runtime.query_experiment_run(
                admission.identities.evaluation_attempt_ref
            )
            is None
        )
    finally:
        runtime.close()


def test_research_graph_rejects_direct_bundle_target_experiment_admission(
    tmp_path: Path,
) -> None:
    provider = _DeterministicExperimentProvider()
    runtime = _runtime(tmp_path / "rg-direct-target-admission", provider)
    try:
        quest = _confirm_direct_quest(runtime)
        intent = ExperimentIntent(
            execution_request_ref="bundle-target-forged-direct-rg-admission",
            quest_ref=quest["quest_ref"],
            title="formal Target namespace at RG boundary",
            hypothesis="RG must independently reject the formal Target namespace.",
            variant_parameter=-0.25,
            sample_count=16,
        )
        runtime_binding = provider.runtime_binding()
        definition_binding = _accept_test_asset(
            runtime,
            name="rg-direct-admission-definition.json",
            content=canonical_json(
                experiment_definition_document(intent, runtime_binding)
            ).encode("utf-8"),
        )
        implementation_binding = _accept_test_asset(
            runtime,
            name="rg-direct-admission-implementation.json",
            content=provider.implementation_bundle(),
        )
        before_counts = _experiment_write_counts(runtime)
        before_snapshot = runtime.owners.research_graph.query_snapshot()
        before_feed = runtime.feed.current_revision()

        with pytest.raises(
            OwnerConflict,
            match="bundle_target_experiment_write_forbidden",
        ):
            runtime.owners.research_graph.admit_experiment(
                intent=intent,
                runtime_binding=runtime_binding,
                definition_binding=definition_binding,
                implementation_binding=implementation_binding,
                idempotency_key="reject-rg-direct-target-admission",
            )

        assert _experiment_write_counts(runtime) == before_counts
        assert runtime.owners.research_graph.query_snapshot() == before_snapshot
        assert runtime.feed.current_revision() == before_feed
    finally:
        runtime.close()


@pytest.mark.parametrize(
    "legacy_marker",
    ("execution_request_prefix", "bundle_target_ref"),
)
def test_historical_bundle_target_experiment_rows_are_diagnostic_only(
    tmp_path: Path,
    legacy_marker: str,
) -> None:
    root = tmp_path / f"agent-runtime-historical-target-row-{legacy_marker}"
    provider = _DeterministicExperimentProvider()
    runtime = _runtime(root, provider)
    admitted = runtime.experiment.start(
        ExperimentIntent(
            execution_request_ref="standalone-request-before-history-seed",
            quest_ref=_confirm_direct_quest(runtime)["quest_ref"],
            title="historical row seed",
            hypothesis="A legacy Target row must never become executable again.",
            variant_parameter=-0.25,
            sample_count=16,
        ),
        "historical-target-row-seed",
    )
    run = runtime.owners.agent_runtime.query_experiment_run(
        admitted["identities"]["evaluation_attempt_ref"]
    )
    assert run is not None
    with runtime._database.write() as connection:
        if legacy_marker == "execution_request_prefix":
            connection.execute(
                text(
                    "UPDATE ar_experiment_runs SET execution_request_ref = "
                    "'bundle-target-historical-ar-row' WHERE run_ref = :run_ref"
                ),
                {"run_ref": run.run_ref},
            )
        else:
            connection.execute(
                text(
                    "UPDATE ar_experiment_runs SET execution_request_ref = "
                    "'historical-nonprefix-request', bundle_target_ref = "
                    "'historical-bundle-target-ref' WHERE run_ref = :run_ref"
                ),
                {"run_ref": run.run_ref},
            )
    before_restart = _agent_runtime_experiment_storage(runtime)
    assert runtime.owners.agent_runtime.query_active_experiment_run() is None
    assert runtime.owners.agent_runtime.claim_next_experiment() is None
    assert _agent_runtime_experiment_storage(runtime) == before_restart
    runtime.close()

    recovered = _runtime(root, provider)
    try:
        assert _agent_runtime_experiment_storage(recovered) == before_restart
        with recovered._database.write() as connection:
            connection.execute(
                text(
                    "UPDATE ar_experiment_runs SET status = 'running' "
                    "WHERE run_ref = :run_ref"
                ),
                {"run_ref": run.run_ref},
            )
            connection.execute(
                text(
                    "UPDATE ar_experiment_attempts SET status = 'running' "
                    "WHERE attempt_ref = :attempt_ref"
                ),
                {"attempt_ref": run.attempt_ref},
            )
        before_mutations = _agent_runtime_experiment_storage(recovered)
        blocked_mutations = (
            lambda: recovered.owners.agent_runtime.record_experiment_observation(
                run_ref=run.run_ref,
                attempt_ref=run.attempt_ref,
                fence_ref=run.fence_ref,
                kind="stdout",
                payload={"line": "must not be recorded"},
                observed_at=1_720_000_202.0,
            ),
            lambda: recovered.owners.agent_runtime.complete_experiment_execution(
                run_ref=run.run_ref,
                attempt_ref=run.attempt_ref,
                fence_ref=run.fence_ref,
                result={},
            ),
            lambda: recovered.owners.agent_runtime.retry_experiment_execution(
                run_ref=run.run_ref,
                attempt_ref=run.attempt_ref,
                fence_ref=run.fence_ref,
                failure_code="experiment_provider_timeout",
            ),
            lambda: recovered.owners.agent_runtime.fail_experiment_execution(
                run_ref=run.run_ref,
                attempt_ref=run.attempt_ref,
                fence_ref=run.fence_ref,
                failure_code="historical_target_row_forbidden",
            ),
            lambda: recovered.owners.agent_runtime.defer_experiment_reconciliation(
                run_ref=run.run_ref,
                attempt_ref=run.attempt_ref,
                fence_ref=run.fence_ref,
                reason_code="experiment_provider_reconciliation_pending",
            ),
            lambda: recovered.owners.agent_runtime.replace_experiment_execution(
                run.evaluation_attempt_ref
            ),
            lambda: recovered.owners.agent_runtime.begin_provider_unit(
                unit_ref="historical-target-provider-unit",
                operation_ref="historical-target-operation",
                run_ref=run.run_ref,
                attempt_ref=run.attempt_ref,
                fence_ref=run.fence_ref,
                unit_kind="experiment",
            ),
            lambda: recovered.owners.agent_runtime.acknowledge_provider_safe_point(
                run_ref=run.run_ref,
                attempt_ref=run.attempt_ref,
                fence_ref=run.fence_ref,
            ),
        )
        for mutate in blocked_mutations:
            with pytest.raises(
                OwnerConflict,
                match="bundle_target_experiment_write_forbidden",
            ):
                mutate()
            assert _agent_runtime_experiment_storage(recovered) == before_mutations
    finally:
        recovered.close()


@pytest.mark.parametrize(
    "suffix,metrics",
    (
        (
            "positive",
            {"baseline_mean": 0.0, "variant_mean": 0.25, "mean_delta": 0.25},
        ),
        (
            "negative",
            {
                "baseline_mean": 0.0,
                "variant_mean": -0.25,
                "mean_delta": -0.25,
            },
        ),
        (
            "zero",
            {"baseline_mean": 0.0, "variant_mean": 0.0, "mean_delta": 0.0},
        ),
    ),
)
def test_complete_measurement_acceptance_is_independent_of_result_sign(
    tmp_path: Path,
    suffix: str,
    metrics: dict[str, float],
) -> None:
    runtime = _runtime(
        tmp_path / f"experiment-formal-{suffix}",
        _MetricsExperimentProvider(metrics),
    )
    try:
        quest = _confirm_direct_quest(runtime)
        admitted = runtime.experiment.start(
            ExperimentIntent(
                execution_request_ref=f"formal-{suffix}-request",
                quest_ref=quest["quest_ref"],
                title=f"formal {suffix}",
                hypothesis="完整性门禁不使用结果符号。",
                variant_parameter=metrics["mean_delta"],
                sample_count=16,
            ),
            f"formal-{suffix}-request",
        )
        assert _drain_experiments(runtime) == 3
        accepted = runtime.experiment.query(
            admitted["identities"]["evaluation_attempt_ref"]
        )
        assert accepted["formal_measurement"]["status"] == "accepted"
        assert accepted["formal_measurement"]["metric_result"]["metrics"] == metrics
    finally:
        runtime.close()


@pytest.mark.parametrize("invalid_value", (float("nan"), float("inf")))
def test_non_finite_metrics_fail_execution_without_metric_result(
    tmp_path: Path, invalid_value: float
) -> None:
    runtime = _runtime(
        tmp_path / f"experiment-non-finite-{invalid_value}",
        _MetricsExperimentProvider(
            {
                "baseline_mean": 0.0,
                "variant_mean": invalid_value,
                "mean_delta": invalid_value,
            }
        ),
    )
    try:
        quest = _confirm_direct_quest(runtime)
        admitted = runtime.experiment.start(
            ExperimentIntent(
                execution_request_ref="formal-non-finite-request",
                quest_ref=quest["quest_ref"],
                title="non finite",
                hypothesis="NaN 和 Inf 不能进入正式测量。",
                variant_parameter=-0.25,
                sample_count=16,
            ),
            "formal-non-finite-request",
        )
        assert _drain_experiments(runtime) == 1
        failed = runtime.experiment.query(
            admitted["identities"]["evaluation_attempt_ref"]
        )
        assert failed["execution"]["status"] == "failed"
        assert failed["execution"]["failure"] == {
            "code": "experiment_metric_invalid"
        }
        assert failed["formal_measurement"]["metric_result"] is None
    finally:
        runtime.close()


def test_partial_attempts_cannot_merge_metrics_across_attempts(
    tmp_path: Path,
) -> None:
    provider = _SequenceMetricsProvider(
        (
            {"baseline_mean": 0.0, "variant_mean": -0.25},
            {"mean_delta": -0.25},
        )
    )
    runtime = _runtime(tmp_path / "experiment-partial-attempts", provider)
    try:
        quest = _confirm_direct_quest(runtime)
        base = ExperimentIntent(
            execution_request_ref="partial-attempt-one",
            quest_ref=quest["quest_ref"],
            title="partial attempt",
            hypothesis="两个 Attempt 的部分指标不能拼成一个 MetricResult。",
            variant_parameter=-0.25,
            sample_count=16,
        )
        first = runtime.experiment.start(base, "partial-attempt-one")
        second = runtime.experiment.start(
            replace(base, execution_request_ref="partial-attempt-two"),
            "partial-attempt-two",
        )
        assert _drain_experiments(runtime) == 6
        for admitted in (first, second):
            measured = runtime.experiment.query(
                admitted["identities"]["evaluation_attempt_ref"]
            )
            assert measured["formal_measurement"]["status"] == "rejected"
            assert measured["formal_measurement"]["metric_result"] is None
    finally:
        runtime.close()


def test_real_execution_assets_and_formal_measurement_remain_layered(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path / "experiment-measurement")
    try:
        quest = _confirm_direct_quest(runtime)
        intent = ExperimentIntent(
            execution_request_ref="semantic-measurement-request",
            quest_ref=quest["quest_ref"],
            title="负向结果仍可接纳",
            hypothesis="固定偏移可以为负，符号不构成接纳门禁。",
            variant_parameter=-0.25,
            sample_count=16,
        )
        admitted = runtime.experiment.start(intent, "measurement-start")
        assert admitted["execution_request"]["definition"]["receipt"]["issuer"] == (
            "research_memory"
        )
        assert admitted["execution_request"]["receipt"]["issuer"] == "research_graph"

        assert runtime.experiment.process_once()
        executed = runtime.experiment.query(
            admitted["identities"]["evaluation_attempt_ref"]
        )
        assert executed["execution"]["status"] == "executed"
        assert executed["execution"]["execution_receipt"]["subject_ref"] == (
            executed["execution"]["attempt_ref"]
        )
        assert [event["kind"] for event in executed["execution"]["events"]] == [
            "status",
            "stdout",
            "telemetry",
            "status",
        ]
        assert executed["assets"]["status"] == "not_attempted"
        assert executed["formal_measurement"] == {
            "status": "not_attempted",
            "metric_result": None,
        }

        assert runtime.experiment.process_once()
        assets_accepted = runtime.experiment.query(
            admitted["identities"]["evaluation_attempt_ref"]
        )
        assert assets_accepted["assets"]["status"] == "accepted"
        assert len(assets_accepted["assets"]["checkpoint_artifacts"]) == 1
        assert len(assets_accepted["assets"]["log_assets"]) == 1
        assert len(assets_accepted["assets"]["analysis_assets"]) == 1
        assert assets_accepted["assets"]["result_content"] is not None
        for role in (
            *assets_accepted["assets"]["checkpoint_artifacts"],
            *assets_accepted["assets"]["log_assets"],
            *assets_accepted["assets"]["analysis_assets"],
            assets_accepted["assets"]["result_content"],
        ):
            assert role["asset_receipt"]["issuer"] == "research_memory"
            assert role["receipt"]["issuer"] == "research_graph"
        assert assets_accepted["formal_measurement"]["status"] == "not_attempted"
        run = runtime.owners.agent_runtime.query_experiment_run(
            admitted["identities"]["evaluation_attempt_ref"]
        )
        assert run is not None
        assert run.result_hash is not None
        assert run.execution_receipt is not None
        manifest = runtime.owners.agent_runtime.verify_experiment_execution_receipt(
            run_ref=run.run_ref,
            attempt_ref=run.attempt_ref,
            fence_ref=run.fence_ref,
            evaluation_attempt_ref=run.evaluation_attempt_ref,
            result_hash=run.result_hash,
            receipt=run.execution_receipt,
        )
        assert manifest.checkpoint_content_hashes == tuple(
            role["content_hash"]
            for role in assets_accepted["assets"]["checkpoint_artifacts"]
        )
        assert manifest.log_content_hash == assets_accepted["assets"][
            "log_assets"
        ][0]["content_hash"]
        assert manifest.analysis_content_hash == assets_accepted["assets"][
            "analysis_assets"
        ][0]["content_hash"]
        assert manifest.result_content_hash == assets_accepted["assets"][
            "result_content"
        ]["content_hash"]
        all_events = runtime.experiment.query_events(
            admitted["identities"]["evaluation_attempt_ref"],
            after_sequence=0,
            limit=512,
        )
        assert manifest.observation_content_hash == hashlib.sha256(
            canonical_json(list(all_events)).encode("utf-8")
        ).hexdigest()

        assert runtime.experiment.process_once()
        accepted = runtime.experiment.query(
            admitted["identities"]["evaluation_attempt_ref"]
        )
        measurement = accepted["formal_measurement"]
        assert measurement["status"] == "accepted"
        assert measurement["metric_result"]["metrics"] == {
            "baseline_mean": 0.0,
            "variant_mean": -0.25,
            "mean_delta": -0.25,
        }
        assert measurement["metric_result"]["receipt"]["subject_ref"] == (
            admitted["identities"]["evaluation_attempt_ref"]
        )
        assert runtime.experiment.process_once() is False
    finally:
        runtime.close()
