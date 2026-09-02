from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path
from types import MethodType, SimpleNamespace

import pytest

from meta_research.owners.common import OwnerConflict
from meta_research.owners.common import AcceptanceReceipt, canonical_hash, canonical_json
from meta_research.bundle_protocol import projection_plain_value
from meta_research.owners.research_graph import (
    TARGET_COMMIT_RECEIPT_KIND,
    SQLiteResearchGraph,
    _receipt_hash,
)
from test_bundle_completion_contract import _closure
from test_public_bundle_stage import (
    _DeterministicBundleSkill,
    _DeterministicPlanSkill,
    _TwoTargetBundleSkill,
    _bundle_runtime,
    _finish_plan_stage,
    _prepare_bundle_request,
)


class _InitiallySealedBundleSkill(_DeterministicBundleSkill):
    """Isolate report completion from Target execution in count-negative tests."""

    def _target_plan(self, request):
        target_plan = super()._target_plan(request)
        target_plan["initial_strategy_update"]["strategy_complete"] = True
        return target_plan


class _EpochPlanSkill(_DeterministicPlanSkill):
    """Keep the test provider's native Session identity unique per Plan Run."""

    def __init__(self) -> None:
        super().__init__(no_gap=False)
        self._session_ordinal = 0

    def generate_draft(self, request):
        draft = super().generate_draft(request)
        self._session_ordinal += 1
        return replace(
            draft,
            primary_session_ref=f"plan-primary-{self._session_ordinal}",
        )



def _current_bundle_request(runtime):
    current = runtime.bundle_stage.query_current()
    request_value = current["stage_run_request"]
    assert isinstance(request_value, dict)
    request = runtime.owners.advancement_engine.query_bundle_stage_request(
        request_value["cycle_ref"]
    )
    assert request is not None and request.accepted_formal_plan is not None
    return request


def _seed_source_only_report_for_replan(
    database: Path,
    *,
    request_ref: str,
    run_ref: str,
    attempt_ref: str,
    fence_ref: str,
) -> tuple[str, str, AcceptanceReceipt]:
    report_ref = "bundle_report_replan_owner_boundaries"
    report_hash = canonical_hash({"fixture": "replan-report"})
    receipt_hash = canonical_hash({"fixture": "replan-report-receipt"})
    empty_refs_json = canonical_json([])
    empty_refs_hash = canonical_hash([])
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute(
            "INSERT INTO ar_bundle_reports (report_ref, run_ref, ordinal, "
            "request_ref, attempt_ref, fence_ref, formal_plan_ref, "
            "plan_document_hash, formal_plan_content_receipt_ref, "
            "formal_plan_content_receipt_hash, target_graph_ref, "
            "target_graph_generation, target_set_hash, coverage_hash, "
            "target_graph_receipt_ref, target_graph_receipt_hash, "
            "target_refs_json, target_refs_hash, notice_refs_json, "
            "notice_refs_hash, handoff_manifest_refs_json, "
            "handoff_manifest_refs_hash, target_commit_receipts_json, "
            "target_commit_receipts_hash, report_json, report_hash, "
            "disposition, idempotency_key, request_hash, receipt_ref, "
            "receipt_hash, accepted_at) VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?, "
            "?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
            "'replan_required', ?, ?, ?, ?, 23.0)",
            (
                report_ref,
                run_ref,
                request_ref,
                attempt_ref,
                fence_ref,
                "formal_plan_replan_fixture",
                canonical_hash({"fixture": "plan-document"}),
                "formal_plan_source_receipt_replan_fixture",
                canonical_hash({"fixture": "source-receipt"}),
                "target_graph_replan_fixture",
                canonical_hash({"fixture": "target-set"}),
                canonical_hash({"fixture": "coverage"}),
                "target_graph_receipt_replan_fixture",
                canonical_hash({"fixture": "graph-receipt"}),
                empty_refs_json,
                empty_refs_hash,
                empty_refs_json,
                empty_refs_hash,
                empty_refs_json,
                empty_refs_hash,
                empty_refs_json,
                empty_refs_hash,
                canonical_json({}),
                report_hash,
                "replan-report-owner-boundary-fixture",
                canonical_hash({"fixture": "report-request"}),
                "bundle_report_receipt_replan_fixture",
                receipt_hash,
            ),
        )
        connection.commit()
    return (
        report_ref,
        report_hash,
        AcceptanceReceipt(
            issuer="agent_runtime",
            kind="bundle_report_accepted",
            receipt_ref="bundle_report_receipt_replan_fixture",
            subject_ref=report_ref,
            payload_hash=receipt_hash,
        ),
    )


def _install_replan_report_verifier(
    monkeypatch: pytest.MonkeyPatch,
    runtime,
    *,
    report_ref: str,
    report_hash: str,
):
    accepted = SimpleNamespace(
        report=SimpleNamespace(disposition="replan_required"),
        report_hash=report_hash,
    )

    def verify(
        _self,
        *,
        request,
        run_ref,
        bundle_report_ref,
        bundle_report_receipt,
        expected_disposition=None,
    ):
        assert request.stage == "bundle"
        assert run_ref
        assert bundle_report_ref == report_ref
        assert bundle_report_receipt.subject_ref == report_ref
        assert expected_disposition in {None, "replan_required"}
        return accepted

    monkeypatch.setattr(
        runtime.owners.advancement_engine,
        "_verify_bundle_report_for_advancement",
        MethodType(verify, runtime.owners.advancement_engine),
    )
    return accepted


def test_plan_document_acceptance_is_hash_subject_restart_safe_and_tamper_closed(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "formal-plan-content"
    runtime = _bundle_runtime(data_root)
    _prepare_bundle_request(runtime)
    request = _current_bundle_request(runtime)
    formal_plan = request.accepted_formal_plan
    assert formal_plan is not None

    accepted = runtime.owners.research_graph.accept_formal_plan_content(
        formal_plan_ref=formal_plan.formal_plan_ref,
        idempotency_key="bundle-formal-plan-content",
    )
    assert accepted.plan_document_hash == formal_plan.plan_document_hash
    assert accepted.receipt.subject_ref == formal_plan.plan_document_hash
    assert accepted.formal_plan_receipt.subject_ref == formal_plan.formal_plan_ref
    assert accepted.plan_content_receipt.subject_ref == formal_plan.content_ref
    assert len(
        {
            accepted.receipt.receipt_ref,
            accepted.formal_plan_receipt.receipt_ref,
            accepted.plan_content_receipt.receipt_ref,
        }
    ) == 3
    runtime.close()

    restarted = _bundle_runtime(data_root)
    replay = restarted.owners.research_graph.query_formal_plan_content_acceptance(
        formal_plan.formal_plan_ref
    )
    assert replay == accepted
    restarted.close()

    with sqlite3.connect(data_root / "meta-research.sqlite3") as connection:
        connection.execute(
            "UPDATE rg_formal_plan_content_acceptances SET receipt_hash = ? "
            "WHERE acceptance_ref = ?",
            ("0" * 64, accepted.acceptance_ref),
        )

    tampered = _bundle_runtime(data_root)
    try:
        with pytest.raises(
            OwnerConflict,
            match="formal_plan_content_receipt_invalid",
        ):
            tampered.owners.research_graph.query_formal_plan_content_acceptance(
                formal_plan.formal_plan_ref
            )
    finally:
        tampered.close()


def test_bundle_run_completion_rejects_legacy_target_graph_or_count(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _bundle_runtime(
        tmp_path / "legacy-count-rejected",
        bundle_skill_provider=_InitiallySealedBundleSkill(),
    )
    try:
        _prepare_bundle_request(runtime)
        assert runtime.bundle_stage.process_once()
        request = _current_bundle_request(runtime)
        run = runtime.owners.agent_runtime.query_bundle_stage_run(request.request_ref)
        assert run is not None

        with pytest.raises(OwnerConflict, match="bundle_report_required"):
            runtime.owners.agent_runtime.complete_bundle_run(
                run_ref=run.run_ref,
                attempt_ref=run.attempt_ref,
                fence_ref=run.fence_ref,
                target_graph_ref="target_graph_count_is_not_completion",
                decision_receipt=request.receipt,
                idempotency_key="legacy-target-count-completion",
            )

        assert (
            runtime.owners.agent_runtime.query_bundle_stage_run(request.request_ref)
            == run
        )
        assert (
            runtime.owners.advancement_engine.query_bundle_stage_commit(
                request.request_ref
            )
            is None
        )

        graph = None
        projection = None
        for _step in range(10):
            runtime.bundle_stage.process_once()
            graph = runtime.owners.research_graph.query_target_graph(
                request.request_ref
            )
            if graph is not None:
                projection = (
                    runtime.owners.research_graph.query_target_formal_plan_projection(
                        graph_ref=graph.graph_ref
                    )
                )
                if projection is not None:
                    break
        assert graph is not None and graph.strategy_complete is True
        assert projection is not None and graph.targets

        # Even an apparent one-for-one TargetCommit count is only planning
        # evidence.  Without AR terminal handoffs and a canonical BundleReport,
        # the worker must leave both RunCompletion and StageCommit untouched.
        fake_commits = tuple(
            SimpleNamespace(
                commit_ref=f"legacy_count_commit_{ordinal}",
                target_ref=target.target_ref,
            )
            for ordinal, target in enumerate(graph.targets, start=1)
        )
        monkeypatch.setattr(
            runtime.owners.research_graph,
            "query_target_commits",
            lambda _graph_ref: fake_commits,
        )
        monkeypatch.setattr(
            runtime.owners.research_graph,
            "query_plan_evidence_catalog",
            lambda *, quest_ref: (
                0,
                tuple(
                    {"target_commit_root_ref": commit.commit_ref}
                    for commit in fake_commits
                ),
            ),
        )
        monkeypatch.setattr(
            runtime.owners.research_graph,
            "query_target_frontier",
            lambda _graph_ref: (),
        )

        def forbidden_completion(**_values):
            raise AssertionError("TargetCommit count authorized RunCompletion")

        def forbidden_stage_commit(**_values):
            raise AssertionError("TargetCommit count authorized StageCommit")

        monkeypatch.setattr(
            runtime.owners.agent_runtime,
            "complete_bundle_run",
            forbidden_completion,
        )
        monkeypatch.setattr(
            runtime.owners.advancement_engine,
            "commit_bundle_stage",
            forbidden_stage_commit,
        )
        assert runtime.bundle_stage.process_once() is False
        current_run = runtime.owners.agent_runtime.query_bundle_stage_run(
            request.request_ref
        )
        assert current_run is not None and current_run.completion is None
        assert (
            runtime.owners.advancement_engine.query_bundle_stage_commit(
                request.request_ref
            )
            is None
        )
    finally:
        runtime.close()


def test_bundle_worker_accepts_source_and_projection_as_separate_boundaries(
    tmp_path: Path,
) -> None:
    runtime = _bundle_runtime(tmp_path / "formal-plan-production-boundaries")
    try:
        _prepare_bundle_request(runtime)
        request = _current_bundle_request(runtime)
        graph = None
        for _step in range(8):
            assert runtime.bundle_stage.process_once()
            graph = runtime.owners.research_graph.query_target_graph(
                request.request_ref
            )
            if graph is not None:
                break
        assert graph is not None
        assert (
            runtime.owners.research_graph.query_formal_plan_content_acceptance(
                graph.formal_plan_ref
            )
            is None
        )
        assert (
            runtime.owners.research_graph.query_target_formal_plan_projection(
                graph_ref=graph.graph_ref
            )
            is None
        )

        assert runtime.bundle_stage.process_once()
        source = (
            runtime.owners.research_graph.query_formal_plan_content_acceptance(
                graph.formal_plan_ref
            )
        )
        assert source is not None
        assert source.receipt.subject_ref == source.plan_document_hash
        assert (
            runtime.owners.research_graph.query_target_formal_plan_projection(
                graph_ref=graph.graph_ref
            )
            is None
        )

        assert runtime.bundle_stage.process_once()
        projection = (
            runtime.owners.research_graph.query_target_formal_plan_projection(
                graph_ref=graph.graph_ref
            )
        )
        assert projection is not None
        assert projection.source_acceptance_receipt == source.receipt
        assert projection.receipt.subject_ref == projection.projection_digest
        assert projection.projection_digest != source.plan_document_hash
        assert projection.receipt.receipt_ref != source.receipt.receipt_ref
    finally:
        runtime.close()


def test_bundle_report_rejects_handoff_subset_when_rg_has_sibling_commit(
    tmp_path: Path,
) -> None:
    """A blocker/barrier handoff cannot hide an RG-accepted sibling commit."""

    data_root = tmp_path / "bundle-report-complete-commit-set"
    runtime = _bundle_runtime(
        data_root,
        bundle_skill_provider=_TwoTargetBundleSkill(),
    )
    try:
        _prepare_bundle_request(runtime)
        request = _current_bundle_request(runtime)
        graph = None
        for _step in range(8):
            assert runtime.bundle_stage.process_once()
            graph = runtime.owners.research_graph.query_target_graph(
                request.request_ref
            )
            if graph is not None:
                break
        assert graph is not None and len(graph.targets) == 2
        measurements = tuple(
            _closure(
                target.target_ref,
                ("experiment-complete-commit-set",),
                f"measurement-cell-{ordinal}",
            )
            for ordinal, target in enumerate(graph.targets, start=1)
        )
        with sqlite3.connect(data_root / "meta-research.sqlite3") as connection:
            for target, measurement in zip(
                graph.targets, measurements, strict=True
            ):
                closure_document = {
                    "schema_ref": "meta-research/target-commit-closure/v3",
                    "fixture": "commit-set-gate",
                    "target_ref": target.target_ref,
                }
                closure_hash = canonical_hash(closure_document)
                receipt_bindings = {
                    "target_ref": target.target_ref,
                    "target_run_ref": measurement.target_run_ref,
                    "evaluation_attempt_ref": (
                        measurement.evaluation_attempt_ref
                    ),
                    "target_spec_hash": target.spec_hash,
                    "closure_hash": closure_hash,
                    "result_disposition": "positive",
                }
                receipt_hash = _receipt_hash(
                    TARGET_COMMIT_RECEIPT_KIND,
                    measurement.target_commit_ref,
                    receipt_bindings,
                )
                connection.execute(
                    "INSERT INTO rg_target_commits (commit_ref, target_ref, "
                    "target_run_ref, evaluation_attempt_ref, target_spec_hash, "
                    "closure_json, closure_hash, result_disposition, receipt_ref, "
                    "receipt_hash, committed_at) VALUES (?, ?, ?, ?, ?, ?, ?, "
                    "'positive', ?, ?, 22.0)",
                    (
                        measurement.target_commit_ref,
                        target.target_ref,
                        measurement.target_run_ref,
                        measurement.evaluation_attempt_ref,
                        target.spec_hash,
                        canonical_json(closure_document),
                        closure_hash,
                        measurement.rg_target_commit_receipt.receipt_ref,
                        receipt_hash,
                    ),
                )
            connection.commit()

        with pytest.raises(
            OwnerConflict,
            match="bundle_report_target_commit_invalid",
        ):
            runtime.owners.research_graph.verify_bundle_report_target_commits(
                graph_ref=graph.graph_ref,
                closures=(measurements[0],),
                receipts=None,
                head_receipt=graph.head_receipt,
            )
    finally:
        runtime.close()


def test_formal_v3_exposes_no_legacy_experiment_commit_write() -> None:
    assert not hasattr(
        SQLiteResearchGraph,
        "accept_target_commit_from_measurement_closure",
    )


def test_bundle_report_rejects_legacy_or_non_exact_target_commit_closure(
    tmp_path: Path,
) -> None:
    """A TargetCommit subset can never stand in for the terminal cell."""

    runtime = _bundle_runtime(tmp_path / "formal-v3-report-evidence")
    try:
        _prepare_bundle_request(runtime)
        request = _current_bundle_request(runtime)
        graph = None
        for _step in range(8):
            assert runtime.bundle_stage.process_once()
            graph = runtime.owners.research_graph.query_target_graph(
                request.request_ref
            )
            if graph is not None:
                break
        assert graph is not None and graph.targets
        target = graph.targets[0]
        if (
            runtime.owners.research_graph.query_formal_plan_content_acceptance(
                graph.formal_plan_ref
            )
            is None
        ):
            runtime.owners.research_graph.accept_formal_plan_content(
                formal_plan_ref=graph.formal_plan_ref,
                idempotency_key="report-candidate-plan-source",
            )
        if (
            runtime.owners.research_graph.query_target_formal_plan_projection(
                graph_ref=graph.graph_ref
            )
            is None
        ):
            runtime.owners.research_graph.accept_target_formal_plan_projection(
                graph_ref=graph.graph_ref,
                idempotency_key="report-candidate-plan-projection",
            )
        candidate_projection = (
            runtime.owners.research_graph.accept_target_candidate_projection(
                target_ref=target.target_ref,
                idempotency_key="report-target-candidate-projection",
            )
        )
        candidate_projection_document = projection_plain_value(
            candidate_projection
        )
        measurement = _closure(
            target.target_ref,
            ("experiment-formal-v3",),
            "measurement-cell-formal-v3",
        )

        cases: list[tuple[str, dict[str, object]]] = []
        for legacy_version in ("v1", "v2"):
            cases.append(
                (
                    f"legacy-{legacy_version}",
                    {
                        "schema_ref": (
                            "meta-research/target-commit-closure/"
                            + legacy_version
                        ),
                        "accepted_measurement": projection_plain_value(
                            measurement
                        ),
                        "target_candidate_projection": (
                            candidate_projection_document
                        ),
                        "target_execution_closure": {},
                    },
                )
            )
        for field in (
            "code_review",
            "implementation_provenance_refs",
            "checkpoint_artifact_refs",
        ):
            accepted_measurement = json.loads(
                canonical_json(projection_plain_value(measurement))
            )
            if field == "code_review":
                accepted_measurement[field]["disposition"] = "accepted"
            else:
                accepted_measurement[field].append(f"forged-{field}")
            cases.append(
                (
                    f"drift-{field}",
                    {
                        "schema_ref": "meta-research/target-commit-closure/v3",
                        "accepted_measurement": accepted_measurement,
                        "target_candidate_projection": (
                            candidate_projection_document
                        ),
                        "target_execution_closure": {},
                    },
                )
            )
        cases.append(
            (
                "missing-target-candidate-projection",
                {
                    "schema_ref": "meta-research/target-commit-closure/v3",
                    "accepted_measurement": projection_plain_value(measurement),
                    "target_execution_closure": {},
                },
            )
        )
        tampered_candidate_projection = json.loads(
            canonical_json(candidate_projection_document)
        )
        tampered_candidate_projection["candidate"]["local_label"] = (
            "tampered-candidate"
        )
        cases.append(
            (
                "tampered-target-candidate-projection",
                {
                    "schema_ref": "meta-research/target-commit-closure/v3",
                    "accepted_measurement": projection_plain_value(measurement),
                    "target_candidate_projection": tampered_candidate_projection,
                    "target_execution_closure": {},
                },
            )
        )

        for label, closure_document in cases:
            closure_hash = canonical_hash(closure_document)
            receipt_bindings = {
                "target_ref": target.target_ref,
                "target_run_ref": measurement.target_run_ref,
                "evaluation_attempt_ref": measurement.evaluation_attempt_ref,
                "target_spec_hash": target.spec_hash,
                "closure_hash": closure_hash,
                "result_disposition": "positive",
            }
            receipt_hash = _receipt_hash(
                TARGET_COMMIT_RECEIPT_KIND,
                measurement.target_commit_ref,
                receipt_bindings,
            )
            with sqlite3.connect(
                tmp_path
                / "formal-v3-report-evidence"
                / "meta-research.sqlite3"
            ) as connection:
                connection.execute(
                    "DELETE FROM rg_target_commits WHERE target_ref = ?",
                    (target.target_ref,),
                )
                connection.execute(
                    "INSERT INTO rg_target_commits (commit_ref, target_ref, "
                    "target_run_ref, evaluation_attempt_ref, target_spec_hash, "
                    "closure_json, closure_hash, result_disposition, receipt_ref, "
                    "receipt_hash, committed_at) VALUES (?, ?, ?, ?, ?, ?, ?, "
                    "'positive', ?, ?, 22.0)",
                    (
                        measurement.target_commit_ref,
                        target.target_ref,
                        measurement.target_run_ref,
                        measurement.evaluation_attempt_ref,
                        target.spec_hash,
                        canonical_json(closure_document),
                        closure_hash,
                        measurement.rg_target_commit_receipt.receipt_ref,
                        receipt_hash,
                    ),
                )
                connection.commit()
            receipt = AcceptanceReceipt(
                issuer="research_graph",
                kind=TARGET_COMMIT_RECEIPT_KIND,
                receipt_ref=(
                    measurement.rg_target_commit_receipt.receipt_ref
                ),
                subject_ref=measurement.target_commit_ref,
                payload_hash=receipt_hash,
            )
            with pytest.raises(
                OwnerConflict,
                match="bundle_report_target_commit_invalid",
            ):
                runtime.owners.research_graph.verify_bundle_report_target_commits(
                    graph_ref=graph.graph_ref,
                    closures=(measurement,),
                    receipts=(receipt,),
                    head_receipt=graph.head_receipt,
                )
    finally:
        runtime.close()


def test_replan_disposition_retires_exact_run_before_epoch_activation_and_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AE -> AR -> AE is three durable, restart-reconcilable boundaries."""

    data_root = tmp_path / "bundle-replan-owner-boundaries"
    plan_skill = _EpochPlanSkill()
    runtime = _bundle_runtime(data_root, plan_skill_provider=plan_skill)
    try:
        _prepare_bundle_request(runtime)
        assert runtime.bundle_stage.process_once()
        request = _current_bundle_request(runtime)
        run = runtime.owners.agent_runtime.query_bundle_stage_run(
            request.request_ref
        )
        assert run is not None
        report_ref, report_hash, report_receipt = (
            _seed_source_only_report_for_replan(
                data_root / "meta-research.sqlite3",
                request_ref=request.request_ref,
                run_ref=run.run_ref,
                attempt_ref=run.attempt_ref,
                fence_ref=run.fence_ref,
            )
        )
        _install_replan_report_verifier(
            monkeypatch,
            runtime,
            report_ref=report_ref,
            report_hash=report_hash,
        )
        foreground_before = runtime.owners.advancement_engine.query_foreground(
            request.accepted_question.quest_ref
        )
        assert foreground_before is not None
        disposition = (
            runtime.owners.advancement_engine.record_bundle_report_disposition(
                request_ref=request.request_ref,
                run_ref=run.run_ref,
                bundle_report_ref=report_ref,
                bundle_report_receipt=report_receipt,
                idempotency_key="record-replan-before-retirement",
            )
        )
        assert disposition.status == "pending_run_retirement"
        assert disposition.next_stage == "plan"
        assert disposition.next_epoch == request.epoch + 1
        foreground_pending = runtime.owners.advancement_engine.query_foreground(
            request.accepted_question.quest_ref
        )
        assert foreground_pending is not None
        assert {
            key: foreground_pending[key]
            for key in ("cycle_ref", "stage", "epoch", "status")
        } == {
            key: foreground_before[key]
            for key in ("cycle_ref", "stage", "epoch", "status")
        }
        with pytest.raises(OwnerConflict, match="stage_run_request_not_current"):
            runtime.owners.agent_runtime.admit_bundle_stage(
                request,
                "pending-replan-old-scope-effect",
                runtime_binding=run.runtime_binding,
            )
        assert runtime.owners.agent_runtime.query_bundle_replan_run_retirement(
            disposition.disposition_ref
        ) is None
    finally:
        runtime.close()

    restarted = _bundle_runtime(data_root, plan_skill_provider=plan_skill)
    try:
        _install_replan_report_verifier(
            monkeypatch,
            restarted,
            report_ref=report_ref,
            report_hash=report_hash,
        )
        disposition = (
            restarted.owners.advancement_engine.query_bundle_report_disposition(
                report_ref
            )
        )
        assert disposition is not None
        active_before = restarted.owners.agent_runtime.query_snapshot().facts[
            "active_run_count"
        ]
        retirement = restarted.owners.agent_runtime.retire_bundle_run_for_replan(
            disposition_ref=disposition.disposition_ref,
            disposition_receipt=disposition.receipt,
            idempotency_key="retire-replan-exact-run",
        )
        assert retirement.request_ref == request.request_ref
        assert retirement.run_ref == run.run_ref
        assert retirement.attempt_ref == run.attempt_ref
        assert retirement.fence_ref == run.fence_ref
        assert retirement.report_ref == report_ref
        assert retirement.receipt.subject_ref == canonical_hash(
            {
                "run_ref": run.run_ref,
                "attempt_ref": run.attempt_ref,
                "fence_ref": run.fence_ref,
            }
        )
        assert restarted.owners.agent_runtime.query_managed_run(run.run_ref)[
            "status"
        ] == "terminated"
        assert (
            restarted.owners.agent_runtime.query_snapshot().facts[
                "active_run_count"
            ]
            == active_before - 1
        )
        replay = restarted.owners.agent_runtime.retire_bundle_run_for_replan(
            disposition_ref=disposition.disposition_ref,
            disposition_receipt=disposition.receipt,
            idempotency_key="retire-replan-exact-run",
        )
        assert replay == retirement
        foreground_retired = restarted.owners.advancement_engine.query_foreground(
            request.accepted_question.quest_ref
        )
        assert foreground_retired is not None
        assert {
            key: foreground_retired[key]
            for key in ("cycle_ref", "stage", "epoch", "status")
        } == {
            key: foreground_before[key]
            for key in ("cycle_ref", "stage", "epoch", "status")
        }
    finally:
        restarted.close()

    activated_runtime = _bundle_runtime(
        data_root,
        plan_skill_provider=plan_skill,
    )
    try:
        _install_replan_report_verifier(
            monkeypatch,
            activated_runtime,
            report_ref=report_ref,
            report_hash=report_hash,
        )
        disposition = (
            activated_runtime.owners.advancement_engine.query_bundle_report_disposition(
                report_ref
            )
        )
        assert disposition is not None
        retirement = (
            activated_runtime.owners.agent_runtime.query_bundle_replan_run_retirement(
                disposition.disposition_ref
            )
        )
        assert retirement is not None
        activation = (
            activated_runtime.owners.advancement_engine.activate_bundle_replan(
                disposition_ref=disposition.disposition_ref,
                retirement_ref=retirement.retirement_ref,
                retirement_receipt=retirement.receipt,
                idempotency_key="activate-replan-next-epoch",
            )
        )
        assert activation.source_epoch == request.epoch
        assert activation.next_epoch == request.epoch + 1
        foreground_after = (
            activated_runtime.owners.advancement_engine.query_foreground(
                request.accepted_question.quest_ref
            )
        )
        assert foreground_after is not None
        assert foreground_after["stage"] == "plan"
        assert foreground_after["epoch"] == request.epoch + 1
        assert not activated_runtime.bundle_stage.process_once()
        assert activated_runtime.plan_stage.process_once()
        next_plan_request = (
            activated_runtime.owners.advancement_engine.query_plan_stage_request(
                request.cycle_ref
            )
        )
        assert next_plan_request is not None
        assert next_plan_request.epoch == request.epoch + 1
        assert next_plan_request.request_ref != request.request_ref
        second_plan = _finish_plan_stage(activated_runtime)
        assert second_plan["stage_commit"] is not None
        assert activated_runtime.bundle_stage.process_once()
        next_bundle_request = (
            activated_runtime.owners.advancement_engine.query_bundle_stage_request(
                request.cycle_ref
            )
        )
        assert next_bundle_request is not None
        assert next_bundle_request.request_ref != request.request_ref
        assert next_bundle_request.epoch > request.epoch
        activation_replay = (
            activated_runtime.owners.advancement_engine.activate_bundle_replan(
                disposition_ref=disposition.disposition_ref,
                retirement_ref=retirement.retirement_ref,
                retirement_receipt=retirement.receipt,
                idempotency_key="activate-replan-next-epoch",
            )
        )
        assert activation_replay == activation
    finally:
        activated_runtime.close()


def test_blocked_report_keeps_bundle_current_and_allows_later_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Blocked is a durable wait fact, not a terminal Stage disposition."""

    data_root = tmp_path / "bundle-blocked-report-resume"
    runtime = _bundle_runtime(data_root)
    try:
        _prepare_bundle_request(runtime)
        assert runtime.bundle_stage.process_once()
        request = _current_bundle_request(runtime)
        run = runtime.owners.agent_runtime.query_bundle_stage_run(
            request.request_ref
        )
        assert run is not None
        first_ref, first_hash, first_receipt = (
            _seed_source_only_report_for_replan(
                data_root / "meta-research.sqlite3",
                request_ref=request.request_ref,
                run_ref=run.run_ref,
                attempt_ref=run.attempt_ref,
                fence_ref=run.fence_ref,
            )
        )
        second_ref = "bundle_report_blocked_owner_facts_changed"
        second_hash = canonical_hash({"fixture": "blocked-after-owner-change"})
        second_receipt_hash = canonical_hash(
            {"fixture": "blocked-after-owner-change-receipt"}
        )
        with sqlite3.connect(data_root / "meta-research.sqlite3") as connection:
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.execute(
                "UPDATE ar_bundle_reports SET disposition = 'blocked' WHERE "
                "report_ref = ?",
                (first_ref,),
            )
            connection.execute(
                "INSERT INTO ar_bundle_reports (report_ref, run_ref, ordinal, "
                "request_ref, attempt_ref, fence_ref, formal_plan_ref, "
                "plan_document_hash, formal_plan_content_receipt_ref, "
                "formal_plan_content_receipt_hash, target_graph_ref, "
                "target_graph_generation, target_set_hash, coverage_hash, "
                "target_graph_receipt_ref, target_graph_receipt_hash, "
                "target_refs_json, target_refs_hash, notice_refs_json, "
                "notice_refs_hash, handoff_manifest_refs_json, "
                "handoff_manifest_refs_hash, target_commit_receipts_json, "
                "target_commit_receipts_hash, report_json, report_hash, "
                "disposition, idempotency_key, request_hash, receipt_ref, "
                "receipt_hash, accepted_at) SELECT ?, run_ref, 2, request_ref, "
                "attempt_ref, fence_ref, formal_plan_ref, plan_document_hash, "
                "formal_plan_content_receipt_ref, "
                "formal_plan_content_receipt_hash, target_graph_ref, "
                "target_graph_generation, target_set_hash, coverage_hash, "
                "target_graph_receipt_ref, target_graph_receipt_hash, "
                "target_refs_json, target_refs_hash, notice_refs_json, "
                "notice_refs_hash, handoff_manifest_refs_json, "
                "handoff_manifest_refs_hash, target_commit_receipts_json, "
                "target_commit_receipts_hash, report_json, ?, 'blocked', ?, ?, "
                "?, ?, 24.0 FROM ar_bundle_reports WHERE report_ref = ?",
                (
                    second_ref,
                    second_hash,
                    "blocked-owner-facts-changed",
                    canonical_hash({"fixture": "second-blocked-request"}),
                    "bundle_report_receipt_blocked_second",
                    second_receipt_hash,
                    first_ref,
                ),
            )
            connection.commit()
        second_receipt = AcceptanceReceipt(
            issuer="agent_runtime",
            kind="bundle_report_accepted",
            receipt_ref="bundle_report_receipt_blocked_second",
            subject_ref=second_ref,
            payload_hash=second_receipt_hash,
        )
        accepted_by_ref = {
            first_ref: SimpleNamespace(
                report=SimpleNamespace(disposition="blocked"),
                report_hash=first_hash,
            ),
            second_ref: SimpleNamespace(
                report=SimpleNamespace(disposition="blocked"),
                report_hash=second_hash,
            ),
        }

        def verify(
            _self,
            *,
            request,
            run_ref,
            bundle_report_ref,
            bundle_report_receipt,
            expected_disposition=None,
        ):
            assert request.stage == "bundle"
            assert run_ref == run.run_ref
            accepted = accepted_by_ref[bundle_report_ref]
            assert bundle_report_receipt.subject_ref == bundle_report_ref
            assert expected_disposition in {None, "blocked"}
            return accepted

        monkeypatch.setattr(
            runtime.owners.advancement_engine,
            "_verify_bundle_report_for_advancement",
            MethodType(verify, runtime.owners.advancement_engine),
        )
        first = runtime.owners.advancement_engine.record_bundle_report_disposition(
            request_ref=request.request_ref,
            run_ref=run.run_ref,
            bundle_report_ref=first_ref,
            bundle_report_receipt=first_receipt,
            idempotency_key="record-first-blocked-report",
        )
        assert first.status == "blocked"
        assert first.next_stage == "bundle"
        assert first.next_epoch == request.epoch
        # A blocked report does not revoke the current request: a normal AR
        # admission replay remains valid while new Owner facts arrive.
        assert (
            runtime.owners.agent_runtime.admit_bundle_stage(
                request,
                "blocked-request-remains-current",
                runtime_binding=run.runtime_binding,
            ).run_ref
            == run.run_ref
        )
        second = runtime.owners.advancement_engine.record_bundle_report_disposition(
            request_ref=request.request_ref,
            run_ref=run.run_ref,
            bundle_report_ref=second_ref,
            bundle_report_receipt=second_receipt,
            idempotency_key="record-second-blocked-report",
        )
        assert second.status == "blocked"
        assert second.report_ref != first.report_ref
        assert (
            runtime.owners.advancement_engine.query_snapshot().facts[
                "bundle_report_disposition_count"
            ]
            == 2
        )
        foreground = runtime.owners.advancement_engine.query_foreground(
            request.accepted_question.quest_ref
        )
        assert foreground is not None
        assert foreground["stage"] == "bundle"
        assert foreground["epoch"] == request.epoch
        assert runtime.owners.advancement_engine.query_bundle_stage_commit(
            request.request_ref
        ) is None
    finally:
        runtime.close()
