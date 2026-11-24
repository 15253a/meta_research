from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import statistics
import time
from types import SimpleNamespace

from alembic import command
import pytest
from sqlalchemy import create_engine, inspect, text

from meta_research.acquisition import (
    AcquisitionPreflightRequest,
    AcquisitionUnavailable,
)
from meta_research.acquisition_root import CodexAcquisitionRootAdapter
from meta_research.bundle_dispatch_recovery import (
    recover_rejected_dispatch,
    recover_rejected_target_batch,
)
from meta_research.bundle_skill import (
    BundleDispatchRequest,
    BundleSkillRequest,
    BundleTargetBatchRequest,
    CodexBundleSkillAdapter,
)
from meta_research.composition import build_production_runtime
from meta_research.context_presentation import (
    stage_context_view,
    stage_position_prompt,
)
from meta_research.database import Database
from meta_research.error_audit import ErrorAuditStore
from meta_research.error_contract import (
    PROVIDER_INSUFFICIENT_BALANCE,
    error_contract_from_legacy,
)
from meta_research.feed import DurableFeed
from meta_research.harness_adapters import (
    HARNESS_CAPABILITIES,
    HarnessTurnEvidence,
)
from meta_research.human_guidance import (
    FrozenGuidanceBinding,
    FrozenGuidanceCut,
    GuidanceOperationIdentity,
    GuidanceRuntimeScope,
    StageGuidanceOperation,
    guidance_prompt,
)
from meta_research.idea_skill import (
    CodexIdeaSkillAdapter,
    IdeaSkillRequest,
)
from meta_research.migration import upgrade_database
from meta_research.owners.agent_runtime_harness import (
    AgentRuntimeHarnessRetryLater,
    SQLiteAgentRuntimeHarness,
    TargetRootCompletionEvidence,
)
from meta_research.owners.common import (
    AcceptanceReceipt,
    OwnerConflict,
    canonical_hash,
    canonical_json,
)
from meta_research.owners.research_memory import AssetIntakeRequest
from meta_research.paths import prepare_data_root
from meta_research.plan_skill import CodexPlanSkillAdapter
from meta_research.quest_goal import (
    EvolutionDecision,
    ResearchRoot,
    WorkDecision,
    WorkHandle,
)
from meta_research.reasoning_skill import (
    CodexReasoningSkillAdapter,
)
from meta_research.semantic_mcp import (
    ROOT_AGENT_COMMON_OPERATION_IDS,
    SemanticMcpError,
    SemanticMcpGateway,
    SemanticOperation,
)
from meta_research.semantic_owner_gateway import (
    ROOT_AGENT_SEMANTIC_OPERATION_IDS,
    TARGET_ROOT_SEMANTIC_OPERATION_IDS,
)
from meta_research.target_commit_evidence import TargetCommitEvidenceCatalog
from meta_research.target_run_finalizer import TargetRunFinalizer
from meta_research.target_run_runtime import TargetRunRuntime

import test_bundle_input_drift_recovery as bundle_drift_fixtures
import test_bundle_skill_adapter as bundle_adapter_fixtures
import test_idea_skill_contract as idea_fixtures
import test_plan_skill_adapter as plan_fixtures
import test_reasoning_skill_adapter as reasoning_fixtures
from bundle_signed_runner import _SignedDispatchRunner
from guidance_confirmation_helpers import prepare_guidance_submission
from migration_support import schema_head
from test_acquisition_root_workflow import (
    _AcquisitionDelegate,
    _SequenceRunner as _AcqSequenceRunner,
    _fake_codex as _acq_fake_codex,
)
from test_dataset_effect_scope_recovery import _scope
from test_harness_target_root import _TargetRootAdapter, _target_request
from test_harness_target_root_recovery import (
    _Clock,
    _owner_with_active_root,
    _receipt_store,
    _record_proven_failure,
)
from test_public_bundle_stage import (
    _TwoGapPlanSkill,
    _bundle_runtime,
    _confirm_direct_quest,
    _finish_idea_stage,
    _finish_plan_stage,
    _formal_candidate,
)
from test_public_human_collaboration_web import _authenticated_client
from test_public_human_reply_delivery import _login, _open
from test_public_idea_stage import _DeterministicIdeaSkill
from test_public_plan_stage import _DeterministicPlanSkill
from test_root_material_processing import _feedback
from test_root_workspace import (
    _accepted,
    _call,
    _channel,
    _context,
    _external_context,
    _make as _make_material_runtime,
)
from test_bundle_report_read_snapshot import _accept_report
from test_bundle_report_receipt_order import committed_targets
from test_target_launch_admission import _ready_launch
from test_target_root_finalizer import (
    _admit_independent_target_root,
    _completion_handoff,
    _current_bundle_runtime,
    _replace_completion_evidence,
    _root_finalizer_fixture,
)


class _FlexibleEvidenceReader:
    def verify_target_root_completion_evidence(self, *, handle, evidence) -> str:
        if evidence.target_ref != handle.target_ref:
            raise OwnerConflict("target_root_completion_evidence_invalid")
        return canonical_hash({
            "handle": handle.target_ref,
            "evidence_ref": evidence.evidence_ref,
            "operation_ref": evidence.operation_ref,
            "workspace_ref": evidence.workspace_ref,
            "final_text_sha256": evidence.final_text_sha256,
        })


def _new_finalizer(runtime, *, reader=None, evidence=None, lifecycle=None):
    graph = runtime.owners.research_graph
    return TargetRunFinalizer(
        lifecycle=lifecycle or runtime.target_run_finalizer._lifecycle,
        memory=runtime.target_run_finalizer._memory,
        workspace_resolver=runtime.target_run_authorities.agent_runtime,
        evidence_reader=reader or _FlexibleEvidenceReader(),
        measurement_authority=graph,
        graph_authority=graph,
    )


def _fresh_evidence(workspace: Path, base_evidence, *, gen: int, text: str | None = None):
    summary = text or f"Target revision {gen} handoff."
    return replace(
        base_evidence,
        operation_ref=f"{base_evidence.target_run_ref}:op:{gen}",
        operation_generation=gen,
        evidence_ref=f"{base_evidence.target_ref}:ev:{gen}",
        evidence_sequence=gen * 10,
        observed_at=float(gen),
        final_text=summary,
        final_text_sha256=hashlib.sha256(summary.encode("utf-8")).hexdigest(),
    )


def _assert_no_rg_formal_rows(runtime) -> None:
    with runtime._database.read() as connection:
        for table in (
            "rg_variant_runs",
            "rg_evaluation_attempts",
            "rg_metric_results",
            "rg_target_root_measurements",
            "rg_target_commits",
        ):
            assert connection.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one() == 0


def test_target_result_json_remaining_conflicts_route_to_revision_feedback_and_keep_cross_quest_blocked(
    tmp_path: Path,
) -> None:
    def _case(sub: str, mutate_workspace):
        runtime, lifecycle, _memory, authority, handle, workspace, evidence = _root_finalizer_fixture(
            tmp_path / sub
        )
        try:
            finalizer = TargetRunFinalizer(
                lifecycle=lifecycle,
                memory=runtime.target_run_finalizer._memory,
                workspace_resolver=runtime.target_run_authorities.agent_runtime,
                evidence_reader=_FlexibleEvidenceReader(),
                measurement_authority=runtime.owners.research_graph,
                graph_authority=runtime.owners.research_graph,
            )
            mutate_workspace(runtime, authority, handle, workspace, evidence)
            result = finalizer.finalize(handle=handle, evidence=evidence)
            assert result.status == "revision_required", (sub, result)
            assert result.rejection_feedback and result.rejection_feedback.strip(), sub
            _assert_no_rg_formal_rows(runtime)
            return result
        finally:
            runtime.close()

    def _unpaired_eval(_runtime, _auth, _handle, workspace, _ev):
        p = workspace / "outputs/result.json"
        doc = json.loads(p.read_text(encoding="utf-8"))
        doc["formal_runs"][0]["evaluations"] = [
            {"attempt_key": "mismatched", "evaluation_attempt_ref": "eval_attempt_missing_metric", "metrics": doc["metrics"]}
        ]
        p.write_text(canonical_json(doc), encoding="utf-8")

    r1 = _case("unpaired-eval", _unpaired_eval)
    assert r1.pending_code == "target_formal_reused_evaluation_invalid"

    def _unknown_variant(_runtime, _auth, _handle, workspace, _ev):
        p = workspace / "outputs/result.json"
        doc = json.loads(p.read_text(encoding="utf-8"))
        doc["formal_runs"][0]["variant_ref"] = "variant-nonexistent-typo"
        p.write_text(canonical_json(doc), encoding="utf-8")

    r2 = _case("unknown-variant", _unknown_variant)
    assert r2.pending_code == "target_formal_variant_definition_invalid"

    def _unknown_eval(_runtime, _auth, _handle, workspace, _ev):
        p = workspace / "outputs/result.json"
        doc = json.loads(p.read_text(encoding="utf-8"))
        doc["formal_runs"][0]["evaluations"][0]["evaluation_ref"] = "evaluation-nonexistent-typo"
        p.write_text(canonical_json(doc), encoding="utf-8")

    r3 = _case("unknown-eval", _unknown_eval)
    assert r3.pending_code == "target_formal_primary_definition_invalid"

    def _missing_reused_primary(_runtime, _auth, _handle, workspace, _ev):
        p = workspace / "outputs/result.json"
        doc = json.loads(p.read_text(encoding="utf-8"))
        doc["formal_runs"][0]["variant_run_ref"] = "variant-run-does-not-exist"
        p.write_text(canonical_json(doc), encoding="utf-8")

    r4 = _case("missing-reused-primary", _missing_reused_primary)
    assert r4.pending_code == "target_formal_reused_run_invalid"

    def _primary_eval_mismatch(_runtime, auth, _handle, workspace, _ev):
        p = workspace / "outputs/result.json"
        doc = json.loads(p.read_text(encoding="utf-8"))
        doc["formal_runs"][0]["evaluations"][0]["evaluation_ref"] = auth.identities.evaluation_ref + "-foreign"
        p.write_text(canonical_json(doc), encoding="utf-8")

    r5 = _case("primary-eval-mismatch", _primary_eval_mismatch)
    assert r5.pending_code == "target_formal_primary_definition_invalid"

    def _bad_ckpt_role(_runtime, _auth, _handle, workspace, _ev):
        p = workspace / "outputs/result.json"
        doc = json.loads(p.read_text(encoding="utf-8"))
        doc["formal_runs"][0]["checkpoint_paths"] = ["outputs/checkpoints/does-not-exist.pt"]
        p.write_text(canonical_json(doc), encoding="utf-8")

    r6 = _case("bad-ckpt-role", _bad_ckpt_role)
    assert r6.pending_code in {
        "target_root_commit_domain_invalid",
        "target_formal_checkpoint_paths_invalid",
        "target_formal_checkpoint_reference_not_bound",
    }
    assert "checkpoint_paths are not bound" in (r6.rejection_feedback or "")

    runtime_ok, lifecycle_ok, memory_ok, _auth_ok, handle_ok, ws_ok, ev_ok = _root_finalizer_fixture(
        tmp_path / "reused-source"
    )
    try:
        fin_ok = TargetRunFinalizer(
            lifecycle=lifecycle_ok,
            memory=runtime_ok.target_run_finalizer._memory,
            workspace_resolver=runtime_ok.target_run_authorities.agent_runtime,
            evidence_reader=_FlexibleEvidenceReader(),
            measurement_authority=runtime_ok.owners.research_graph,
            graph_authority=runtime_ok.owners.research_graph,
        )
        accepted_ok = fin_ok.finalize(handle=handle_ok, evidence=ev_ok)
        assert accepted_ok.status == "completed"
        facts = runtime_ok.owners.research_graph.query_target_formal_results(handle_ok.target_ref)
        prior_vr_ref = facts[0]["variant_run"]["variant_run_ref"]
        prior_variant_ref = facts[0]["variant_run"]["variant_ref"]
    finally:
        runtime_ok.close()

    rt_foreign, lc_foreign, _m_foreign, _a_foreign, h_foreign, ws_foreign, ev_foreign = _root_finalizer_fixture(
        tmp_path / "foreign-quest"
    )
    try:
        p = ws_foreign / "outputs/result.json"
        doc = json.loads(p.read_text(encoding="utf-8"))
        doc["formal_runs"][0]["variant_ref"] = prior_variant_ref
        p.write_text(canonical_json(doc), encoding="utf-8")
        with rt_foreign._database.write() as conn:
            conn.execute(text(
                "INSERT INTO rg_quests (quest_ref, initialization_id, draft_revision, draft_hash, "
                "proposal_ref, proposal_hash, preview_ref, preview_hash, goal_json, confirmation_ref, "
                "confirmation_hash, receipt_ref, receipt_hash, accepted_at) "
                "VALUES ('quest:foreign','init:foreign',1,'a','prop:f','a','prev:f','a','{}','conf:f','a','rcpt:f','a',1.0)"
            ))
            bh = "b" * 64
            vh = "c" * 64
            conn.execute(text(
                "INSERT INTO rg_experiment_baselines (baseline_ref, quest_ref, forward_contract_json, forward_contract_hash, accepted_at) "
                "VALUES ('base:foreign','quest:foreign','{}',:bh,1.0)"
            ), {"bh": bh})
            conn.execute(text(
                "INSERT INTO rg_experiment_variants (variant_ref, baseline_ref, recipe_hash, recipe_json, accepted_at) "
                "VALUES ('var:foreign','base:foreign',:vh,'{}',1.0)"
            ), {"vh": vh})
        doc["formal_runs"][0]["variant_ref"] = "var:foreign"
        p.write_text(canonical_json(doc), encoding="utf-8")
        fin_foreign = TargetRunFinalizer(
            lifecycle=lc_foreign,
            memory=rt_foreign.target_run_finalizer._memory,
            workspace_resolver=rt_foreign.target_run_authorities.agent_runtime,
            evidence_reader=_FlexibleEvidenceReader(),
            measurement_authority=rt_foreign.owners.research_graph,
            graph_authority=rt_foreign.owners.research_graph,
        )
        with pytest.raises(OwnerConflict, match="target_formal_variant_scope_invalid"):
            fin_foreign.finalize(handle=h_foreign, evidence=ev_foreign)
    finally:
        rt_foreign.close()

    outside_dir = tmp_path / "outside-target"
    outside_dir.mkdir()
    (outside_dir / "leak.log").write_text("external\n", encoding="utf-8")

    def _symlink_intermediate(_runtime, _auth, _handle, workspace, _ev):
        sub = workspace / "outputs" / "sublink"
        try:
            sub.symlink_to(outside_dir, target_is_directory=True)
        except OSError:
            pytest.skip("symlinks unsupported")
        p = workspace / "outputs/result.json"
        doc = json.loads(p.read_text(encoding="utf-8"))
        doc["retained_artifacts"].append({"role": "analysis", "relative_path": "outputs/sublink/leak.log"})
        p.write_text(canonical_json(doc), encoding="utf-8")

    r_sym = _case("symlink-intermediate", _symlink_intermediate)
    assert r_sym.pending_code == "target_root_artifact_symlink_forbidden"

    def _hardlink_artifact(_runtime, _auth, _handle, workspace, _ev):
        extra = workspace / "outputs" / "analysis"
        extra.mkdir(parents=True, exist_ok=True)
        src = extra / "orig.txt"
        src.write_text("data\n", encoding="utf-8")
        hl = extra / "hardlink.txt"
        os.link(src, hl)
        p = workspace / "outputs/result.json"
        doc = json.loads(p.read_text(encoding="utf-8"))
        doc["retained_artifacts"].append({"role": "analysis", "relative_path": "outputs/analysis/hardlink.txt"})
        p.write_text(canonical_json(doc), encoding="utf-8")

    r_hl = _case("hardlink-artifact", _hardlink_artifact)
    assert r_hl.pending_code == "target_root_artifact_type_unsupported"

    from meta_research.bundle_protocol import BundleProtocolError
    from meta_research.owners.research_graph import _measurement_closure_conflict
    from meta_research.target_content_issue import TargetContentIssue

    cap_err = _measurement_closure_conflict(
        "target_root_measurement_closure_invalid",
        BundleProtocolError(
            "too big",
            {"category": "technical_capacity", "path": "$", "actual_bytes": 999999, "limit": 65536},
        ),
    )
    assert type(cap_err) is OwnerConflict and not isinstance(cap_err, TargetContentIssue)
    assert cap_err.feedback and "Do not truncate results" in cap_err.feedback


def test_target_reused_run_conflicts_route_to_revision_feedback(tmp_path: Path) -> None:
    runtime, lifecycle, _memory, _authority, handle, workspace, evidence = _root_finalizer_fixture(
        tmp_path / "reused-run-conflicts"
    )
    try:
        p = workspace / "outputs/result.json"
        doc = json.loads(p.read_text(encoding="utf-8"))
        metrics = doc["metrics"]
        doc["formal_runs"] = [
            {"run_key": "first-run", "artifact_paths": ["logs/train.log"], "evaluations": [{"attempt_key": "e1", "metrics": metrics}]},
        ]
        p.write_text(canonical_json(doc), encoding="utf-8")
        finalizer = TargetRunFinalizer(
            lifecycle=lifecycle,
            memory=runtime.target_run_finalizer._memory,
            workspace_resolver=runtime.target_run_authorities.agent_runtime,
            evidence_reader=_FlexibleEvidenceReader(),
            measurement_authority=runtime.owners.research_graph,
            graph_authority=runtime.owners.research_graph,
        )
        accepted = finalizer.finalize(handle=handle, evidence=evidence)
        assert accepted.status == "completed"
        facts = runtime.owners.research_graph.query_target_formal_results(handle.target_ref)
        vr_ref = facts[0]["variant_run"]["variant_run_ref"]

        from meta_research.formal_entities import register_root_entities
        from meta_research.target_content_issue import TargetContentIssue
        with runtime._database.read() as conn:
            root_row = dict(
                conn.execute(
                    text("SELECT * FROM rg_target_root_measurements WHERE target_ref = :t"),
                    {"t": handle.target_ref},
                ).mappings().one()
            )
            completion_row = dict(
                conn.execute(
                    text("SELECT * FROM ar_target_root_completions WHERE target_ref = :t"),
                    {"t": handle.target_ref},
                ).mappings().one()
            )
            manifest_row = dict(
                conn.execute(
                    text("SELECT * FROM rm_target_root_completion_manifests WHERE manifest_ref = :m"),
                    {"m": accepted.manifest_ref},
                ).mappings().one()
            )
            authority = dict(
                conn.execute(
                    text("SELECT * FROM rg_target_measurement_domain_authorities WHERE target_ref = :t"),
                    {"t": handle.target_ref},
                ).mappings().one()
            )

        def _invoke(bad_payload: dict[str, object], *, verify_only: bool) -> None:
            patched_manifest = {**manifest_row, "result_document_json": canonical_json(bad_payload)}
            with (runtime._database.read() if verify_only else runtime._database.write()) as conn:
                register_root_entities(
                    conn,
                    root=root_row,
                    authority=authority,
                    manifest=patched_manifest,
                    completion=completion_row,
                    commit_ref=accepted.target_commit_ref,
                    verify_only=verify_only,
                    source_owner=runtime.owners.research_graph,
                )

        bad_doc = deepcopy(doc)
        bad_doc["formal_runs"] = [{
            "run_key": "reused-with-impl",
            "variant_run_ref": vr_ref,
            "implementation_paths": ["implementation"],
            "artifact_paths": ["logs/train.log"],
            "evaluations": [],
        }]
        with pytest.raises(TargetContentIssue, match="target_formal_reused_run_input_mismatch") as exc1:
            _invoke(bad_doc, verify_only=False)
        assert exc1.value.feedback

        bad_doc2 = deepcopy(doc)
        bad_doc2["formal_runs"] = [{
            "run_key": "reused-bad-inputs",
            "variant_run_ref": vr_ref,
            "input_refs": ["wrong-input-ref"],
            "artifact_paths": ["logs/train.log"],
            "evaluations": [],
        }]
        with pytest.raises(TargetContentIssue, match="target_formal_reused_run_input_mismatch") as exc2:
            _invoke(bad_doc2, verify_only=False)
        assert exc2.value.feedback

        bad_doc3 = deepcopy(doc)
        bad_doc3["formal_runs"] = [{
            "run_key": "reused-bad-impl-rev",
            "variant_run_ref": vr_ref,
            "implementation_revision_ref": "target_impl_" + ("0" * 64),
            "artifact_paths": ["logs/train.log"],
            "evaluations": [],
        }]
        with pytest.raises(TargetContentIssue, match="target_formal_implementation_revision_mismatch") as exc3:
            _invoke(bad_doc3, verify_only=False)
        assert exc3.value.feedback

        bad_doc4 = deepcopy(doc)
        bad_doc4["formal_runs"] = [{
            "run_key": "reused-bad-artifact",
            "variant_run_ref": vr_ref,
            "artifact_paths": ["logs/does_not_exist.log"],
            "evaluations": [],
        }]
        with pytest.raises(TargetContentIssue, match="target_formal_artifact_path_not_bound") as exc4:
            _invoke(bad_doc4, verify_only=False)
        assert exc4.value.feedback

        with pytest.raises(OwnerConflict) as exc_vo:
            _invoke(bad_doc, verify_only=True)
        assert type(exc_vo.value) is OwnerConflict
    finally:
        runtime.close()


def test_target_completion_successor_unchanged_becomes_revisable_feedback_without_wedge(
    tmp_path: Path,
) -> None:
    runtime, lifecycle, memory, _authority, handle, workspace, evidence = _root_finalizer_fixture(
        tmp_path / "successor-unchanged"
    )
    try:
        result_path = workspace / "outputs/result.json"
        doc = json.loads(result_path.read_text(encoding="utf-8"))
        doc["formal_runs"][0]["input_refs"] = ["not-admitted"]
        result_path.write_text(canonical_json(doc), encoding="utf-8")

        finalizer = TargetRunFinalizer(
            lifecycle=lifecycle,
            memory=runtime.target_run_finalizer._memory,
            workspace_resolver=runtime.target_run_authorities.agent_runtime,
            evidence_reader=_FlexibleEvidenceReader(),
            measurement_authority=runtime.owners.research_graph,
            graph_authority=runtime.owners.research_graph,
        )
        gen1 = finalizer.finalize(handle=handle, evidence=evidence)
        assert gen1.status == "revision_required"
        assert gen1.pending_code == "target_formal_input_reference_invalid"

        ev2 = replace(
            evidence,
            operation_ref="gen2-unchanged-op",
            operation_generation=2,
            evidence_ref="gen2-unchanged-ev",
            evidence_sequence=20,
            observed_at=2.0,
        )
        gen2 = finalizer.finalize(handle=handle, evidence=ev2)
        assert gen2.status == "revision_required"
        assert gen2.pending_code == "target_root_completion_successor_unchanged"
        assert gen2.rejection_feedback and "未检测到改动" in gen2.rejection_feedback or "outputs/result.json" in gen2.rejection_feedback
        intakes_dir = workspace.parent / ".target-completion-intakes"
        if intakes_dir.exists():
            assert list(intakes_dir.iterdir()) == []

        gen1_completion = lifecycle.query_completion_by_ref(gen1.completion_ref)
        with pytest.raises(OwnerConflict, match="target_root_completion_successor_unchanged"):
            lifecycle.accept_completion(
                handle=handle,
                handoff=gen1_completion.handoff,
                harness_operation_ref="direct-unchanged-op",
                evidence_ref="direct-unchanged-ev",
                evidence_content_hash="f" * 64,
                workspace_ref=gen1_completion.workspace_ref,
                implementation_revision_ref=gen1_completion.implementation_revision_ref,
                implementation_tree_hash=gen1_completion.implementation_tree_hash,
                result_document_hash=gen1_completion.result_document_hash,
                artifact_snapshot_hash=gen1_completion.artifact_snapshot_hash,
                idempotency_key="replay-old-op-must-fail",
            )

        doc["formal_runs"][0]["input_refs"] = []
        result_path.write_text(canonical_json(doc), encoding="utf-8")
        ev3 = _fresh_evidence(workspace, evidence, gen=3, text="Fixed formal_runs input_refs.")
        gen3 = finalizer.finalize(handle=handle, evidence=ev3)
        assert gen3.status == "completed"
        assert gen3.completion_generation == 3
    finally:
        runtime.close()


def test_provider_balance_and_auth_circuit_breaker_and_account_refresh_wake(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock(1000.0)
    monkeypatch.setattr("meta_research.owners.agent_runtime_harness.time.time", clock.time)
    monkeypatch.setenv("META_RESEARCH_PROVIDER_HARD_QUOTA_THRESHOLD", "2")
    monkeypatch.setenv("META_RESEARCH_PROVIDER_BALANCE_MAX_BACKOFF_SECONDS", "3600")

    path = tmp_path / "circuit-breaker.sqlite3"
    database, owner, request, _handle = _owner_with_active_root(path)
    try:
        run = owner.query_run(request.request_ref)
        assert run is not None
        op1 = f"{run.run_ref}:harness_turn:1"
        owner.start_operation(
            run_ref=run.run_ref, operation_ref=op1, generation=1, invocation_hash="1" * 64, resume=False
        )
        retry1 = _record_proven_failure(owner, op1, PROVIDER_INSUFFICIENT_BALANCE)
        assert retry1 is not None
        assert retry1.consecutive_failures == 1
        assert retry1.next_retry_at == 1300.0
        assert retry1.circuit_open is False

        clock.now = 1300.0
        reopened = owner.reopen_failed_target_root(request.request_ref)
        assert reopened.reopened is True
        with pytest.raises(AgentRuntimeHarnessRetryLater) as pending_retry:
            owner.reopen_failed_target_root(request.request_ref)
        assert pending_retry.value.retry.failure_code == PROVIDER_INSUFFICIENT_BALANCE
        assert pending_retry.value.retry.next_retry_at == 1600.0

        op2 = f"{run.run_ref}:harness_turn:2"
        owner.start_operation(
            run_ref=run.run_ref, operation_ref=op2, generation=2, invocation_hash="2" * 64, resume=False
        )
        retry2 = _record_proven_failure(owner, op2, PROVIDER_INSUFFICIENT_BALANCE)
        assert retry2 is not None
        assert retry2.consecutive_failures == 2
        assert retry2.circuit_open is True
        assert retry2.next_retry_at == 1900.0

        clock.now = 1310.0
        with pytest.raises(AgentRuntimeHarnessRetryLater):
            owner.reopen_failed_target_root(request.request_ref)
        woken = owner.reopen_failed_target_root(request.request_ref, auth_refreshed_at=1305.0)
        assert woken.reopened is True

        op3 = f"{run.run_ref}:harness_turn:3"
        owner.start_operation(
            run_ref=run.run_ref, operation_ref=op3, generation=3, invocation_hash="3" * 64, resume=False
        )
        retry3 = _record_proven_failure(owner, op3, "provider_auth_revoked")
        assert retry3 is not None
        assert retry3.consecutive_failures == 3
        assert retry3.circuit_open is True
        assert retry3.next_retry_at == clock.now + 1200.0
    finally:
        database.close()


def test_configurable_bounded_rejection_backoff_for_target_and_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock(5000.0)
    monkeypatch.setattr(time, "time", clock.time)
    monkeypatch.setenv("META_RESEARCH_REJECTION_BACKOFF_AFTER", "2")
    monkeypatch.setenv("META_RESEARCH_REJECTION_BACKOFF_BASE_SECONDS", "10.0")
    monkeypatch.setenv("META_RESEARCH_REJECTION_BACKOFF_MAX_SECONDS", "60.0")
    monkeypatch.setenv("META_RESEARCH_REJECTION_MAX_CONSECUTIVE", "4")

    rt = _bundle_runtime(tmp_path / "stage-rej-backoff")
    try:
        quest = _confirm_direct_quest(rt)
        _finish_idea_stage(rt)
        rt.plan_stage.process_once()
        req = rt.owners.advancement_engine.query_plan_stage_request(quest["cycle_ref"])
        assert req is not None
        while (run := rt.owners.agent_runtime.query_plan_stage_run(req.request_ref)) is None:
            assert rt.plan_stage.process_once()
        with rt._database.fenced_write() as conn:
            conn.execute(text("UPDATE ar_stage_attempts SET status = 'rejected', closed_at = 4988.0 WHERE attempt_ref = :a"), {"a": run.attempt_ref})
            conn.execute(text("UPDATE ar_execution_fences SET status = 'rejected', closed_at = 4988.0 WHERE fence_ref = :f"), {"f": run.fence_ref})
            conn.execute(text(
                "INSERT INTO ar_stage_attempts (attempt_ref, run_ref, generation, root_session_ref, fence_ref, predecessor_attempt_ref, status, created_at, closed_at) "
                "VALUES ('plan_attempt_2', :r, 2, :s, 'fence_2', :p, 'rejected', 4990.0, 4995.0)"
            ), {"r": run.run_ref, "p": run.attempt_ref, "s": run.root_session_ref})
            conn.execute(text(
                "INSERT INTO ar_execution_fences (fence_ref, run_ref, attempt_ref, generation, status, issued_at, closed_at) "
                "VALUES ('fence_2', :r, 'plan_attempt_2', 2, 'rejected', 4990.0, 4995.0)"
            ), {"r": run.run_ref})
            conn.execute(text(
                "INSERT INTO ar_stage_attempts (attempt_ref, run_ref, generation, root_session_ref, fence_ref, predecessor_attempt_ref, status, created_at, closed_at) "
                "VALUES ('plan_attempt_3', :r, 3, :s, 'fence_3', 'plan_attempt_2', 'running', 4996.0, NULL)"
            ), {"r": run.run_ref, "s": run.root_session_ref})
            conn.execute(text(
                "INSERT INTO ar_execution_fences (fence_ref, run_ref, attempt_ref, generation, status, issued_at) "
                "VALUES ('fence_3', :r, 'plan_attempt_3', 3, 'current', 4996.0)"
            ), {"r": run.run_ref})
            conn.execute(text("UPDATE ar_stage_runs SET current_attempt_ref='plan_attempt_3', current_fence_ref='fence_3' WHERE run_ref=:r"), {"r": run.run_ref})
            conn.execute(text("UPDATE ar_run_controls SET attempt_ref='plan_attempt_3', fence_ref='fence_3', control_revision=control_revision+1 WHERE run_ref=:r"), {"r": run.run_ref})

        with pytest.raises(OwnerConflict, match="stage_rejection_backoff_pending") as exc_wait:
            rt.owners.agent_runtime.begin_provider_unit(
                unit_ref="u3", operation_ref="op3", run_ref=run.run_ref,
                attempt_ref="plan_attempt_3", fence_ref="fence_3", unit_kind="plan_primary",
            )
        assert exc_wait.value.details["next_retry_at"] == 5005.0

        clock.now = 5005.0
        rt.owners.agent_runtime.begin_provider_unit(
            unit_ref="u3", operation_ref="op3", run_ref=run.run_ref,
            attempt_ref="plan_attempt_3", fence_ref="fence_3", unit_kind="plan_primary",
        )

        with rt._database.fenced_write() as conn:
            conn.execute(text("UPDATE ar_provider_units SET status = 'completed', completed_at = 5006.0 WHERE unit_ref = 'u3'"))
            conn.execute(text("UPDATE ar_stage_attempts SET status = 'rejected', closed_at = 5006.0 WHERE attempt_ref = 'plan_attempt_3'"))
            conn.execute(text("UPDATE ar_execution_fences SET status = 'rejected', closed_at = 5006.0 WHERE fence_ref = 'fence_3'"))
            conn.execute(text(
                "INSERT INTO ar_stage_attempts (attempt_ref, run_ref, generation, root_session_ref, fence_ref, predecessor_attempt_ref, status, created_at, closed_at) "
                "VALUES ('plan_attempt_4', :r, 4, :s, 'fence_4', 'plan_attempt_3', 'rejected', 5007.0, 5008.0)"
            ), {"r": run.run_ref, "s": run.root_session_ref})
            conn.execute(text(
                "INSERT INTO ar_execution_fences (fence_ref, run_ref, attempt_ref, generation, status, issued_at, closed_at) "
                "VALUES ('fence_4', :r, 'plan_attempt_4', 4, 'rejected', 5007.0, 5008.0)"
            ), {"r": run.run_ref})
            conn.execute(text(
                "INSERT INTO ar_stage_attempts (attempt_ref, run_ref, generation, root_session_ref, fence_ref, predecessor_attempt_ref, status, created_at, closed_at) "
                "VALUES ('plan_attempt_5', :r, 5, :s, 'fence_5', 'plan_attempt_4', 'running', 5009.0, NULL)"
            ), {"r": run.run_ref, "s": run.root_session_ref})
            conn.execute(text(
                "INSERT INTO ar_execution_fences (fence_ref, run_ref, attempt_ref, generation, status, issued_at) "
                "VALUES ('fence_5', :r, 'plan_attempt_5', 5, 'current', 5009.0)"
            ), {"r": run.run_ref})
            conn.execute(text("UPDATE ar_stage_runs SET current_attempt_ref='plan_attempt_5', current_fence_ref='fence_5' WHERE run_ref=:r"), {"r": run.run_ref})
            conn.execute(text("UPDATE ar_run_controls SET attempt_ref='plan_attempt_5', fence_ref='fence_5', control_revision=control_revision+1 WHERE run_ref=:r"), {"r": run.run_ref})

        with pytest.raises(OwnerConflict, match="stage_rejection_limit_reached") as limit_exc:
            rt.owners.agent_runtime.begin_provider_unit(
                unit_ref="u5", operation_ref="op5", run_ref=run.run_ref,
                attempt_ref="plan_attempt_5", fence_ref="fence_5", unit_kind="plan_primary",
            )
        assert limit_exc.value.details["operator_required"] is True
        assert limit_exc.value.details["recovery_owner"] == "operator"
        assert isinstance(limit_exc.value.details.get("audit_ref"), str) and limit_exc.value.details["audit_ref"]
        stage_corr = f"stage_run:{run.run_ref}:attempt:plan_attempt_5"
        assert limit_exc.value.details["correlation_ref"] == stage_corr

        monkeypatch.setenv("META_RESEARCH_REJECTION_MAX_CONSECUTIVE", "6")
        clock.now = 5100.0
        rt.owners.agent_runtime.begin_provider_unit(
            unit_ref="u5", operation_ref="op5", run_ref=run.run_ref,
            attempt_ref="plan_attempt_5", fence_ref="fence_5", unit_kind="plan_primary",
        )
    finally:
        rt.close()

    from test_plan_owner_acceptance import _QuestionRestatingPlanSkill
    from test_public_plan_stage import _DeterministicIdeaSkill, _runtime as _plan_runtime

    with monkeypatch.context() as mp:
        mp.setenv("META_RESEARCH_REJECTION_BACKOFF_AFTER", "1")
        mp.setenv("META_RESEARCH_REJECTION_MAX_CONSECUTIVE", "1")
        rt_w = _plan_runtime(
            tmp_path / "stage-worker-rej",
            idea_skill=_DeterministicIdeaSkill(),
            plan_skill=_QuestionRestatingPlanSkill(),
        )
        try:
            q_w = _confirm_direct_quest(rt_w)
            _finish_idea_stage(rt_w)
            req_w = None
            for _ in range(12):
                progressed = rt_w.plan_stage.process_once()
                if req_w is None:
                    req_w = rt_w.owners.advancement_engine.query_plan_stage_request(q_w["cycle_ref"])
                if not progressed and rt_w.plan_stage.transient_error == "stage_rejection_limit_reached":
                    break
            assert req_w is not None
            run_w = rt_w.owners.agent_runtime.query_plan_stage_run(req_w.request_ref)
            assert run_w is not None and run_w.attempt_generation == 2
            assert rt_w.plan_stage.transient_error == "stage_rejection_limit_reached"

            audit_store = ErrorAuditStore(rt_w._database, rt_w.feed)
            corr_w = f"stage_run:{run_w.run_ref}:attempt:{run_w.attempt_ref}"
            audits_w = audit_store.query_correlation(corr_w)
            assert len(audits_w) == 1
            assert audits_w[0].payload["contract"]["details"]["operator_required"] is True
            assert audits_w[0].payload["contract"]["details"]["recovery_owner"] == "operator"

            write_calls: list[str] = []
            from contextlib import contextmanager
            orig_w = rt_w._database.write
            orig_fw = rt_w._database.fenced_write

            @contextmanager
            def _spy_w():
                write_calls.append("write")
                with orig_w() as c:
                    yield c

            @contextmanager
            def _spy_fw():
                write_calls.append("fenced_write")
                with orig_fw() as c:
                    yield c

            with monkeypatch.context() as spy_mp:
                spy_mp.setattr(rt_w._database, "write", _spy_w)
                spy_mp.setattr(rt_w._database, "fenced_write", _spy_fw)
                assert rt_w.plan_stage.process_once() is False
                assert rt_w.plan_stage.transient_error == "stage_rejection_limit_reached"
                assert write_calls == []
            assert len(audit_store.query_correlation(corr_w)) == 1

            ack_w = rt_w.owners.agent_runtime.acknowledge_stage_rejection_limit(
                run_ref=run_w.run_ref, operator_note="operator reviewed plan rejection",
            )
            assert ack_w["acknowledged_up_to_generation"] == 1
            assert rt_w.plan_stage.process_once() is True
            assert rt_w.plan_stage.transient_error is None
        finally:
            rt_w.close()

    rt_t, lc_t, _mem_t, _auth_t, h_t, ws_t, ev_t = _root_finalizer_fixture(tmp_path / "target-rej-backoff")
    try:
        p = ws_t / "outputs/result.json"
        doc = json.loads(p.read_text(encoding="utf-8"))
        doc["formal_runs"][0]["input_refs"] = ["bad-input"]
        p.write_text(canonical_json(doc), encoding="utf-8")
        fin = _new_finalizer(rt_t)
        calls = []

        class _MockHarness:
            def run_or_resume_target_root(self, req_ref, *, prompt, mcp_base_url):
                calls.append((req_ref, prompt))

        t_runtime = TargetRunRuntime(
            agent_runtime=rt_t.target_run_authorities.agent_runtime,
            research_graph=rt_t.owners.research_graph,
            target_graph=rt_t.target_run_authorities.research_graph,
            target_agent=rt_t.target_run_authorities.agent_runtime,
            target_root_lifecycle=lc_t,
            harnesses=_MockHarness(),
            finalizer=fin,
        )
        t_runtime.configure_resident_mcp_endpoint("http://127.0.0.1:8765")
        monkeypatch.setenv("META_RESEARCH_REJECTION_MAX_CONSECUTIVE", "4")
        rej1 = fin.finalize(handle=h_t, evidence=ev_t)
        assert t_runtime._resume_after_rejection(handle=h_t, harness_request_ref="hr:1", revision=rej1) is True
        assert len(calls) == 1

        clock.now += 10.0
        doc["formal_runs"][0]["input_refs"] = ["bad-input-2"]
        p.write_text(canonical_json(doc), encoding="utf-8")
        ev2 = _fresh_evidence(ws_t, ev_t, gen=2)
        rej2 = fin.finalize(handle=h_t, evidence=ev2)
        assert t_runtime._resume_after_rejection(handle=h_t, harness_request_ref="hr:1", revision=rej2) is False
        st2 = t_runtime.query_status(h_t.target_ref)
        assert st2 is not None and st2.phase == "revision_backoff_pending" and st2.next_retry_at is not None

        for gen in (3, 4):
            clock.now += 100.0
            doc["formal_runs"][0]["input_refs"] = [f"bad-input-{gen}"]
            p.write_text(canonical_json(doc), encoding="utf-8")
            evg = _fresh_evidence(ws_t, ev_t, gen=gen)
            rejg = fin.finalize(handle=h_t, evidence=evg)
            res = t_runtime._resume_after_rejection(handle=h_t, harness_request_ref="hr:1", revision=rejg)
            if gen == 4:
                assert res is False
        st4 = t_runtime.query_status(h_t.target_ref)
        assert st4 is not None and st4.phase == "revision_paused"
        assert st4.pending_code == "target_rejection_limit_reached"
        assert st4.operator_required is True
        assert isinstance(st4.audit_ref, str) and st4.audit_ref
        target_corr = f"target_run:{h_t.target_ref}:{h_t.target_run_ref}"
        assert st4.correlation_ref == target_corr
        target_audits = ErrorAuditStore(rt_t._database, rt_t.feed)
        assert target_audits.query(st4.audit_ref) is not None
        assert t_runtime._resume_after_rejection(handle=h_t, harness_request_ref="hr:1", revision=rejg) is False
        assert len(target_audits.query_correlation(target_corr)) == 1
        assert rt_t.owners.agent_runtime.query_human_requests() == ()

        ack_target = lc_t.acknowledge_target_rejection_limit(
            target_ref=h_t.target_ref,
            target_run_ref=h_t.target_run_ref,
            operator_note="operator approved target continuation",
        )
        assert ack_target["acknowledged_up_to_generation"] == 4
        assert t_runtime._resume_after_rejection(handle=h_t, harness_request_ref="hr:1", revision=rejg) is True
        assert len(calls) >= 2
    finally:
        rt_t.close()


def test_plan_and_reasoning_stage_requests_do_zero_disk_io_under_write_lock(
    committed_targets, monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, graph, _bundle_run, _closures, _source, _proj = committed_targets
    quest_ref = graph.quest_ref
    cycle_ref = graph.cycle_ref

    evidence_rev, catalog_tuple = runtime.owners.research_graph.query_plan_evidence_catalog(quest_ref=quest_ref)
    catalog_items = [dict(item) for item in catalog_tuple]
    assert len(catalog_items) >= 1

    in_write = {"depth": 0, "t0": 0.0}
    materialize_in_write = []
    write_durations_ms = []
    original_write = runtime._database.write
    original_materialize = runtime.owners.research_memory.materialize_asset

    from contextlib import contextmanager

    @contextmanager
    def wrapped_write():
        with original_write() as conn:
            in_write["depth"] += 1
            t0 = time.perf_counter()
            try:
                yield conn
            finally:
                write_durations_ms.append((time.perf_counter() - t0) * 1000.0)
                in_write["depth"] -= 1

    def wrapped_materialize(version_ref: str):
        if in_write["depth"] > 0:
            materialize_in_write.append(version_ref)
        return original_materialize(version_ref)

    monkeypatch.setattr(runtime._database, "write", wrapped_write)
    monkeypatch.setattr(runtime.owners.research_memory, "materialize_asset", wrapped_materialize)

    ae = runtime.owners.advancement_engine
    plan_req = ae.query_plan_stage_request(cycle_ref)
    assert plan_req is not None
    base_pack = {k: v for k, v in plan_req.context_pack.items() if k != "quest_goal_revision"}
    for run_idx in range(5):
        epoch = 2 + run_idx
        with original_write() as conn:
            conn.execute(
                text("UPDATE ae_foreground_heads SET stage = 'plan', epoch = :e WHERE quest_ref = :q"),
                {"e": epoch, "q": quest_ref},
            )
        ae.ensure_plan_stage_request(
            cycle_ref=cycle_ref,
            accepted_question=plan_req.accepted_question,
            accepted_idea_set=plan_req.accepted_idea_set,
            context_pack={
                **base_pack,
                "evidence_reference_revision": evidence_rev,
                "evidence_catalog": catalog_items,
            },
            idempotency_key=f"part4-plan-with-catalog-{run_idx}",
        )
    plan_write_ms = list(write_durations_ms)
    write_durations_ms.clear()
    assert materialize_in_write == [], f"materialize_asset called inside Database.write(): {materialize_in_write}"
    print(
        f"\n[BENCH plan_stage_request write_lock] runs={len(plan_write_ms)} "
        f"median_ms={statistics.median(plan_write_ms):.3f} "
        f"min_ms={min(plan_write_ms):.3f} max_ms={max(plan_write_ms):.3f} "
        f"materialize_in_write={len(materialize_in_write)}"
    )


def test_reasoning_stage_request_does_zero_materialize_under_write_lock(
    committed_targets, monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, _graph, bundle_run, accepted = _accept_report(committed_targets)
    agent = runtime.owners.agent_runtime
    ae = runtime.owners.advancement_engine
    completion = agent.complete_bundle_run(
        run_ref=bundle_run.run_ref,
        attempt_ref=bundle_run.attempt_ref,
        fence_ref=bundle_run.fence_ref,
        report_ref=accepted.report_ref,
        decision_receipt=accepted.receipt,
        idempotency_key="part4-reasoning-complete-bundle",
    )
    ae.commit_bundle_stage(
        request_ref=bundle_run.request_ref,
        run_ref=bundle_run.run_ref,
        bundle_report_ref=accepted.report_ref,
        run_completion_receipt=completion.receipt,
        bundle_report_receipt=accepted.receipt,
        idempotency_key="part4-reasoning-advance-bundle",
    )
    bundle_request = ae._query_stage_request_by_ref(bundle_run.request_ref)

    in_write = {"depth": 0}
    materialize_in_write = []
    write_durations_ms = []
    original_write = runtime._database.write
    original_materialize = runtime.owners.research_memory.materialize_asset
    from contextlib import contextmanager

    @contextmanager
    def wrapped_write():
        with original_write() as conn:
            in_write["depth"] += 1
            t0 = time.perf_counter()
            try:
                yield conn
            finally:
                write_durations_ms.append((time.perf_counter() - t0) * 1000.0)
                in_write["depth"] -= 1

    def wrapped_materialize(version_ref: str):
        if in_write["depth"] > 0:
            materialize_in_write.append(version_ref)
        return original_materialize(version_ref)

    monkeypatch.setattr(runtime._database, "write", wrapped_write)
    monkeypatch.setattr(runtime.owners.research_memory, "materialize_asset", wrapped_materialize)

    request = ae.ensure_reasoning_stage_request(
        cycle_ref=bundle_request.cycle_ref,
        accepted_question=bundle_request.accepted_question,
        idempotency_key="part4-reasoning-request-lock",
    )
    assert len(request.context_pack["accepted_target_commit_closures"]) == 2
    assert materialize_in_write == [], f"materialize_asset called inside write(): {materialize_in_write}"
    if write_durations_ms:
        print(
            f"\n[BENCH reasoning_stage_request write_lock] runs={len(write_durations_ms)} "
            f"median_ms={statistics.median(write_durations_ms):.3f} "
            f"min_ms={min(write_durations_ms):.3f} max_ms={max(write_durations_ms):.3f} "
            f"materialize_in_write={len(materialize_in_write)}"
        )


def test_quest_goal_evolve_stop_retention_zero_lock_io_and_binary_managed_assets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from test_human_guidance_providers import _admit_guidance_target, _submit, _supervised_target

    runtime = _current_bundle_runtime(tmp_path / "binary-retention")
    runtime.configure_resident_mcp_endpoint("http://127.0.0.1:8999")
    try:
        target, admission = _admit_guidance_target(runtime)
        launch = runtime.owners.agent_runtime.query_admitted_target_launch(target.target_ref)
        assert launch is not None
        _submit(runtime, launch.quest_ref, strength=5)
        adapter = _supervised_target(runtime, tmp_path)
        invoke = adapter.invoke
        observed: dict[str, object] = {}

        in_fw = {"depth": 0}
        disk_io_in_fw = []
        fw_durations_ms = []
        orig_fw = runtime._database.fenced_write
        orig_read_txt = runtime.owners.research_memory.read_asset_entry_text
        from contextlib import contextmanager

        @contextmanager
        def wrapped_fw():
            with orig_fw() as conn:
                in_fw["depth"] += 1
                t0 = time.perf_counter()
                try:
                    yield conn
                finally:
                    fw_durations_ms.append((time.perf_counter() - t0) * 1000.0)
                    in_fw["depth"] -= 1

        def wrapped_read_txt(version_ref: str):
            if in_fw["depth"] > 0:
                disk_io_in_fw.append(("read_asset_entry_text", version_ref))
            return orig_read_txt(version_ref)

        monkeypatch.setattr(runtime._database, "fenced_write", wrapped_fw)
        monkeypatch.setattr(runtime.owners.research_memory, "read_asset_entry_text", wrapped_read_txt)
        if hasattr(runtime.owners.research_memory, "verify_managed_asset_content"):
            orig_verify = runtime.owners.research_memory.verify_managed_asset_content

            def wrapped_verify(version_ref: str):
                if in_fw["depth"] > 0:
                    disk_io_in_fw.append(("verify_managed_asset_content", version_ref))
                return orig_verify(version_ref)

            monkeypatch.setattr(runtime.owners.research_memory, "verify_managed_asset_content", wrapped_verify)

        def _mcp(token: str, op: str, **args):
            _, resp, _ = runtime.harnesses.dispatch_mcp_http(
                token,
                {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "tools.call", "arguments": {"operation_id": op, "arguments": args}}},
                mcp_session_id=None,
            )
            return resp["result"]

        def retain_binary_then_stop(invocation):
            if invocation.root_kind != "target" or observed:
                return invoke(invocation)
            workspace, ws_path = runtime.target_run_authorities.agent_runtime.read_target_workspace_location(invocation.run_ref)
            ckpt_dir = ws_path / "outputs" / "checkpoints"
            ckpt_dir.mkdir(parents=True, exist_ok=True)
            binary_bytes = b"\x89PNG\r\n\x1a\n\x00\x00\xff\xfeBINARY_CKPT"
            (ckpt_dir / "model.bin").write_bytes(binary_bytes)
            note_dir = ws_path / "outputs" / "analysis"
            note_dir.mkdir(parents=True, exist_ok=True)
            (note_dir / "research-note.md").write_text("note\n", encoding="utf-8")

            note_err = _mcp(
                invocation.mcp_token,
                "research_memory.assets.intake",
                effect_id="note-wrong-selector",
                intake={
                    "source_kind": "local_path",
                    "custody_mode": "managed",
                    "display_name": "note.md",
                    "media_type": "text/markdown",
                    "source": {"kind": "workspace", "workspace_ref": workspace.workspace_ref, "path": "outputs/analysis/research-note.md"},
                },
            )
            assert note_err.get("isError") and "target_note_selector_required" in str(note_err), note_err

            bin_intake = _mcp(
                invocation.mcp_token,
                "research_memory.assets.intake",
                effect_id="bin-ckpt-intake",
                intake={
                    "source_kind": "local_path",
                    "custody_mode": "managed",
                    "display_name": "model.bin",
                    "media_type": "application/octet-stream",
                    "source": {"kind": "workspace", "workspace_ref": workspace.workspace_ref, "path": "outputs/checkpoints/model.bin"},
                },
            )
            assert not bin_intake.get("isError"), bin_intake
            bin_asset = bin_intake["structuredContent"]["asset"]
            bin_binding = runtime.owners.research_memory.query_asset_version(bin_asset["version_ref"])
            runtime.owners.research_graph.accept_asset_role(
                binding=bin_binding.as_binding(),
                role="quest_source_material",
                quest_ref=launch.quest_ref,
                idempotency_key="bin-role",
            )

            inbox = _mcp(invocation.mcp_token, "human_guidance.read")["structuredContent"]
            delivery = inbox["deliveries"][0]
            exact = _mcp(invocation.mcp_token, "human_guidance.read", delivery_ref=delivery["delivery_ref"], effect_id="read-bin-g")["structuredContent"]
            _mcp(
                invocation.mcp_token,
                "human_guidance.feedback",
                delivery_ref=delivery["delivery_ref"],
                effect_id="fb-bin-g",
                understanding="Stop target and keep binary checkpoint.",
                changes="Stopping target.",
                continuing_work="Keep checkpoint.",
                reasons="Checkpoint needed.",
                disposition="goal_alignment_pending",
            )
            cut = _mcp(invocation.mcp_token, "research_graph.quest_goal.read")["structuredContent"]["operation_basis"]
            decisions = [
                {
                    "kind": "stop",
                    "work": w,
                    "reason": "Retain binary checkpoint.",
                    "retention": {
                        "kind": "selected",
                        "assets": [{
                            "asset_ref": bin_asset["asset_ref"],
                            "version_ref": bin_asset["version_ref"],
                            "content_hash": bin_asset["content_hash"],
                            "manifest_hash": bin_asset["manifest_hash"],
                            "receipt": bin_asset["receipt"],
                            "meaning": "Binary checkpoint saved before stop.",
                        }],
                    },
                }
                if w["target_ref"] == target.target_ref
                else {"kind": "continue", "work": w, "reason": "Continue."}
                for w in cut["work"]["items"]
            ]
            decision = {
                "expected_revision": cut["goal"]["goal_revision_ref"],
                "conditions_basis": cut["conditions"]["basis_ref"],
                "work_basis": cut["work"]["basis_ref"],
                "cause": {"kind": "human_guidance", "delivery_ref": delivery["delivery_ref"], "guide_ref": exact["guide_ref"]},
                "replacement": {"goal": "Binary retained goal.", "completion_criteria": "Binary checkpoint preserved."},
                "criteria_review": "Replaced.",
                "judgment": "Retain binary artifact.",
                "conditions": {
                    "runtime_conditions_ref": cut["conditions"]["runtime_conditions"]["revision"],
                    "assessments": [{"condition_ref": c["condition_ref"], "disposition": "preserved", "explanation": "Keep."} for c in cut["conditions"]["enduring"]],
                    "newly_identified": [],
                },
                "arrangements": {"decisions": decisions},
                "following_direction": "Proceed with binary artifact.",
            }

            fw_durations_ms.clear()
            disk_io_in_fw.clear()
            evolved = _mcp(invocation.mcp_token, "research_graph.quest_goal.evolve", effect_id="evolve-bin-stop", decision=decision)
            assert not evolved.get("isError"), evolved
            observed["decision"] = decision
            observed["evolved"] = evolved["structuredContent"]
            observed["evolve_lock_ms"] = list(fw_durations_ms)
            observed["disk_io_in_fw"] = list(disk_io_in_fw)
            return invoke(invocation)

        adapter.invoke = retain_binary_then_stop
        runtime.harnesses.run_or_resume_target_root(
            admission.run.request_ref, prompt="Retain binary checkpoint and stop.", mcp_base_url="http://127.0.0.1:8999"
        )
        assert observed.get("evolved") is not None
        assert observed["disk_io_in_fw"] == [], f"Disk I/O executed inside fenced_write(): {observed['disk_io_in_fw']}"
        lock_ms = observed["evolve_lock_ms"]
        print(
            f"\n[BENCH quest_goal.evolve fenced_write] runs={len(lock_ms)} "
            f"median_ms={statistics.median(lock_ms):.3f} disk_io_in_lock={len(observed['disk_io_in_fw'])}"
        )

        unrelated = runtime.owners.research_memory.submit_asset_intake(
            AssetIntakeRequest(
                source_kind="text",
                custody_mode="managed",
                display_name="unrelated-direct.txt",
                media_type="text/plain",
                content=b"Unrelated asset.\n",
                origin_quest_ref=launch.quest_ref,
            ),
            idempotency_key="unrelated-retain-check",
        ).asset
        runtime.owners.research_graph.accept_asset_role(
            binding=unrelated.as_binding(),
            role="quest_source_material",
            quest_ref=launch.quest_ref,
            idempotency_key="unrelated-role",
        )
        saved_decision = observed["decision"]
        target_work = next(
            d["work"]
            for d in saved_decision["arrangements"]["decisions"]
            if d["work"].get("target_ref") == target.target_ref
        )
        bad_stop = (
            WorkDecision(
                kind="stop",
                work=WorkHandle(**target_work),
                reason="Attempt unrelated asset retention.",
                retention={
                    "kind": "selected",
                    "assets": [{
                        "asset_ref": unrelated.asset_ref,
                        "version_ref": unrelated.version_ref,
                        "content_hash": unrelated.content_hash,
                        "manifest_hash": unrelated.manifest_hash,
                        "receipt": unrelated.receipt.as_public_dict(),
                        "meaning": "Should fail producer check.",
                    }],
                },
            ),
        )
        with runtime._database.read() as conn:
            with pytest.raises(OwnerConflict, match="goal_retained_asset_producer_invalid"):
                runtime.owners.research_graph._verify_goal_retention_in_transaction(
                    conn,
                    quest_ref=launch.quest_ref,
                    decisions=bad_stop,
                )

        with monkeypatch.context() as mp:
            mp.setattr(
                runtime.owners.research_memory._receipt_verifier,
                "_asset_export_source",
                lambda _ref: (None, SimpleNamespace(), [SimpleNamespace(custody_mode="managed")], {"entries": []}),
            )
            with pytest.raises(OwnerConflict, match="asset_custody_unavailable"):
                runtime.owners.research_memory.verify_managed_asset_content("any-empty-manifest")
    finally:
        runtime.close()


def test_quest_goal_initial_revision_lookup_uses_index_not_full_scan(
    tmp_path: Path,
) -> None:
    runtime = _bundle_runtime(tmp_path / "quest-index-lookup")
    try:
        quest = _confirm_direct_quest(runtime)
        init_rev = runtime.owners.research_graph.query_current_quest_goal_revision(quest["quest_ref"])
        assert init_rev is not None
        init_ref = init_rev["goal_revision_ref"]

        with runtime._database.read() as conn:
            plan = [
                row[3]
                for row in conn.execute(
                    text("EXPLAIN QUERY PLAN SELECT * FROM rg_quests WHERE initial_goal_revision_ref = :r"),
                    {"r": init_ref},
                ).all()
            ]
        assert any("USING INDEX uq_rg_quests_initial_goal_revision_ref" in line for line in plan), plan
        found = runtime.owners.research_graph.query_quest_goal_revision(init_ref)
        assert found is not None and found["goal_revision_ref"] == init_ref
    finally:
        runtime.close()


def test_human_guidance_read_pure_paths_skip_fenced_write_while_preserving_resolves_receipts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from test_public_human_collaboration_web import _authenticated_client as _hc_client

    runtime = _bundle_runtime(tmp_path / "guidance-read-lock")
    try:
        quest = _confirm_direct_quest(runtime)
        qref = quest["quest_ref"]
        client, headers = _hc_client(runtime)
        try:
            proposal = runtime.owners.human_collaboration.record_agent_proposal(
                f"quest:{qref}",
                {
                    "proposal_kind": "soft_constraint",
                    "text": "Check whether Quest goal holds.",
                    "assistant_understanding": "Assess goal impact.",
                    "applies_to": ["整个 Quest"],
                    "semantic_scope": {"kind": "quest", "quest_ref": qref},
                    "strength": 3,
                    "preserve_conditions": [],
                    "work_materials": None,
                },
                "guidance-read-test",
            )
            client.post(
                "/api/v1/human-collaboration/guidance",
                json={
                    "scope_ref": f"quest:{qref}",
                    "text": "Check whether Quest goal holds.",
                    "proposal_ref": proposal["proposal_ref"],
                    "expected_proposal_hash": proposal["proposal_hash"],
                },
                headers={**headers, "Idempotency-Key": "g-read-submit"},
            )
        finally:
            client.close()
        _finish_idea_stage(runtime)
        runtime.plan_stage.process_once()
        plan_req = runtime.owners.advancement_engine.query_plan_stage_request(quest["cycle_ref"])
        assert plan_req is not None
        while (run := runtime.owners.agent_runtime.query_plan_stage_run(plan_req.request_ref)) is None:
            assert runtime.plan_stage.process_once()
        hc = runtime.owners.human_collaboration
        runtime.owners.agent_runtime.begin_provider_unit(
            unit_ref="u-g-read-1",
            operation_ref=run.primary_invocation.operation_ref,
            run_ref=run.run_ref,
            attempt_ref=run.attempt_ref,
            fence_ref=run.fence_ref,
            unit_kind="plan_primary",
        )
        op = StageGuidanceOperation(
            scope=GuidanceRuntimeScope(
                root_kind="plan",
                run_ref=run.run_ref,
                attempt_ref=run.attempt_ref,
                root_session_ref=run.root_session_ref,
                fence_ref=run.fence_ref,
                runtime_binding_hash=run.runtime_binding_hash,
            ),
            job_ref=run.primary_invocation.operation_ref,
            operation_name="primary",
        )
        cut = hc.freeze_operation_guidance(op)

        fw_calls = []
        orig_fw = runtime._database.fenced_write
        from contextlib import contextmanager

        @contextmanager
        def count_fw():
            fw_calls.append(True)
            with orig_fw() as conn:
                yield conn

        monkeypatch.setattr(runtime._database, "fenced_write", count_fw)

        inbox = hc.read_operation_guidance(scope=op.scope, binding=cut.binding)
        assert len(fw_calls) == 0, f"List read acquired fenced_write: {len(fw_calls)}"
        dref = inbox["deliveries"][0]["delivery_ref"]

        exact = hc.read_operation_guidance(scope=op.scope, binding=cut.binding, delivery_ref=dref, effect_id="g-read-1")
        assert len(fw_calls) == 1

        replay = hc.read_operation_guidance(scope=op.scope, binding=cut.binding, delivery_ref=dref, effect_id="g-read-1")
        assert replay == exact
        assert len(fw_calls) == 1, f"Idempotent replay acquired fenced_write: {len(fw_calls)}"

        rec = hc.read_operation_guidance(scope=op.scope, binding=cut.binding, delivery_ref=dref, effect_id="g-read-1", reconcile=True)
        assert rec == exact
        assert len(fw_calls) == 1, f"Reconcile acquired fenced_write: {len(fw_calls)}"

        fb1 = hc.feedback_operation_guidance(
            scope=op.scope, binding=cut.binding,
            delivery_ref=dref, effect_id="g-fb-1",
            understanding="Understood.", changes="None yet.",
            continuing_work="Assessing.", reasons="Needs check.",
            disposition="considered", goal_impact="undetermined",
        )
        assert fb1["goal_impact"] == "undetermined"
        exact2 = hc.read_operation_guidance(scope=op.scope, binding=cut.binding, delivery_ref=dref, effect_id="g-read-2")
        assert exact2["goal_assessments"]
        fb2 = hc.feedback_operation_guidance(
            scope=op.scope, binding=cut.binding,
            delivery_ref=dref, effect_id="g-fb-2",
            understanding="Resolved.", changes="Keep goal.",
            continuing_work="Continue.", reasons="Confirmed.",
            disposition="applied", goal_impact="none",
            resolves_receipts=[fb1["receipt"]["receipt_ref"]],
        )
        assert fb2["goal_impact"] == "none"
    finally:
        runtime.close()


def test_migration_0090_to_0091_lossless_idempotent_and_explain_query_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    upgrade = command.upgrade
    root = tmp_path / "mig-0091"
    with monkeypatch.context() as prev:
        prev.setattr(
            command,
            "upgrade",
            lambda config, _rev: upgrade(config, "0090_reasoning_content_retry"),
        )
        rt = _bundle_runtime(root)
        try:
            quest = _confirm_direct_quest(rt)
            _finish_idea_stage(rt)
            _finish_plan_stage(rt)
            db_path = rt._database.path
            expected_init_ref = rt.owners.research_graph.query_current_quest_goal_revision(
                quest["quest_ref"]
            )["goal_revision_ref"]
        finally:
            rt.close()

    hot_queries = {
        "rg_quests_initial": (
            "SELECT * FROM rg_quests WHERE initial_goal_revision_ref = 'x'",
            "uq_rg_quests_initial_goal_revision_ref",
        ),
        "ae_foreground_heads_cycle": (
            "SELECT * FROM ae_foreground_heads WHERE cycle_ref = 'x' AND status = 'active'",
            "ix_ae_foreground_heads_cycle_status",
        ),
        "rg_question_lifecycle_quest": (
            "SELECT * FROM rg_question_lifecycle WHERE quest_ref = 'x' AND status = 'active'",
            "ix_rg_question_lifecycle_quest_status",
        ),
        "rg_target_graphs_quest": (
            "SELECT * FROM rg_target_graphs WHERE quest_ref = 'x'",
            "ix_rg_target_graphs_quest_ref",
        ),
        "ar_target_run_workspaces_target": (
            "SELECT * FROM ar_target_run_workspaces WHERE target_ref = 'x' AND status = 'active' ORDER BY ordinal DESC LIMIT 1",
            "ix_ar_target_run_workspaces_target_status",
        ),
        "hc_guidance_deliveries_constraint": (
            "SELECT * FROM hc_guidance_deliveries WHERE constraint_ref = 'x' ORDER BY created_at, delivery_ref",
            "ix_hc_guidance_deliveries_constraint",
        ),
        "hc_guidance_effects_delivery": (
            "SELECT * FROM hc_guidance_effects WHERE kind = 'read' AND delivery_ref = 'x'",
            "ix_hc_guidance_effects_delivery_kind",
        ),
        "hc_guidance_read_pages_delivery": (
            "SELECT start_offset, end_offset FROM hc_guidance_read_pages WHERE delivery_ref = 'x' ORDER BY start_offset",
            "ix_hc_guidance_read_pages_delivery",
        ),
        "ar_target_frontier_entries_state": (
            "SELECT target_ref FROM ar_target_frontier_entries WHERE state = 'running' ORDER BY target_ref LIMIT 10",
            "ix_ar_target_frontier_entries_state",
        ),
        "rg_goal_work_intents_kind": (
            "SELECT * FROM rg_goal_work_intents WHERE decision_kind IN ('stop', 'do_not_start') ORDER BY created_at, intent_ref",
            "ix_rg_goal_work_intents_kind_created",
        ),
    }

    with sqlite3.connect(db_path) as conn:
        before_rev = conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        assert before_rev == "0090_reasoning_content_retry"
        before_quests = conn.execute("SELECT quest_ref, draft_hash, goal_json FROM rg_quests ORDER BY quest_ref").fetchall()
        before_cycles = conn.execute("SELECT cycle_ref, quest_ref, question_ref FROM ae_cycles ORDER BY cycle_ref").fetchall()
        before_plans = conn.execute("SELECT decision_ref, quest_ref FROM rg_formal_plan_decisions ORDER BY decision_ref").fetchall()
        print("\n[EQP BEFORE 0090]")
        for label, (sql, _idx) in hot_queries.items():
            if label == "rg_quests_initial":
                print(f"  {label}: SELECT * FROM rg_quests -> SCAN rg_quests (no column/index)")
            else:
                eqp = " | ".join(r[3] for r in conn.execute(f"EXPLAIN QUERY PLAN {sql}").fetchall())
                print(f"  {label}: {eqp}")

    upgrade_database(db_path)
    upgrade_database(db_path)

    with sqlite3.connect(db_path) as conn:
        after_rev = conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        assert after_rev == schema_head()
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("PRAGMA quick_check").fetchone() == ("ok",)
        assert conn.execute("SELECT quest_ref, draft_hash, goal_json FROM rg_quests ORDER BY quest_ref").fetchall() == before_quests
        assert conn.execute("SELECT cycle_ref, quest_ref, question_ref FROM ae_cycles ORDER BY cycle_ref").fetchall() == before_cycles
        assert conn.execute("SELECT decision_ref, quest_ref FROM rg_formal_plan_decisions ORDER BY decision_ref").fetchall() == before_plans
        backfilled = conn.execute(
            "SELECT initial_goal_revision_ref FROM rg_quests WHERE quest_ref = ?",
            (quest["quest_ref"],),
        ).fetchone()[0]
        assert backfilled == expected_init_ref
        launch_cols = {r[1] for r in conn.execute("PRAGMA table_info(ar_target_launches)").fetchall()}
        append_cols = {r[1] for r in conn.execute("PRAGMA table_info(rg_target_graph_appends)").fetchall()}
        assert "goal_revision_ref" in launch_cols
        assert "goal_revision_ref" in append_cols
        print("\n[EQP AFTER 0091]")
        for label, (sql, expected_idx) in hot_queries.items():
            eqp = " | ".join(r[3] for r in conn.execute(f"EXPLAIN QUERY PLAN {sql}").fetchall())
            print(f"  {label}: {eqp}")
            assert expected_idx in eqp, (label, expected_idx, eqp)

    from alembic.config import Config
    from importlib.resources import files
    from sqlalchemy import URL, create_engine
    cfg = Config()
    cfg.set_main_option("script_location", str(files("meta_research.migrations")))
    url = URL.create("sqlite+pysqlite", database=str(db_path))
    engine = create_engine(url, future=True)
    try:
        with engine.connect() as conn:
            cfg.attributes["connection"] = conn
            with pytest.raises(RuntimeError, match="forward-only"):
                command.downgrade(cfg, "0090_reasoning_content_retry")
    finally:
        engine.dispose()


def test_pr320_p2_5_readonly_audit_skip_and_p2_6_unauthorized_agent_routing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = Database(tmp_path / "p25-p26.sqlite3")
    upgrade_database(db.path)
    try:
        store = ErrorAuditStore(db, DurableFeed(db))
        fw_count = 0
        orig_fw = db.fenced_write
        from contextlib import contextmanager

        @contextmanager
        def spy_fw():
            nonlocal fw_count
            fw_count += 1
            with orig_fw() as conn:
                yield conn

        monkeypatch.setattr(db, "fenced_write", spy_fw)

        def _readonly_handler(_ctx, _args):
            raise SemanticMcpError("research_query_changed", {"correction": "Re-query from offset 0."})

        def _readonly_operator_handler(_ctx, _args):
            raise SemanticMcpError("persistence_corrupt_record", {"message": "Indexed store corruption detected."})

        def _unauth_effect(_ctx, _args):
            raise SemanticMcpError("quest_goal_root_unauthorized", {"message": "Not a research root."})

        ops = (
            SemanticOperation("ro.test", "rg", "ro", _readonly_handler, {"type": "object"}, {"type": "object"}, "read"),
            SemanticOperation("ro.operator_fail", "rg", "ro op", _readonly_operator_handler, {"type": "object"}, {"type": "object"}, "read"),
            SemanticOperation(
                "eff.unauth",
                "rg",
                "eff",
                _unauth_effect,
                {"type": "object", "properties": {"effect_id": {"type": "string"}}, "required": ["effect_id"]},
                {"type": "object"},
                "effect",
                "eff.unauth.reconcile",
            ),
            SemanticOperation("eff.unauth.reconcile", "rg", "rec", lambda _c, _a: {}, {"type": "object"}, {"type": "object"}, "reconcile"),
        )
        gw = SemanticMcpGateway(ops, error_audit_store=store)
        ch, _ = gw.issue_channel(
            run_ref="r1", attempt_ref="a1", root_session_ref="s1", fence_ref="f1",
            capability_binding_hash="a" * 64, operation_ids=("ro.test", "ro.operator_fail", "eff.unauth", "eff.unauth.reconcile"),
            root_kind="idea", phase="primary",
        )

        _, ro_resp = gw.dispatch(ch.token, {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "ro.test", "arguments": {}}})
        ro_details = ro_resp["result"]["structuredContent"]["details"]
        assert ro_details["audit_ref"] is None
        assert ro_details["audit_status"] == "skipped_read_only"
        assert fw_count == 0

        _, op_resp = gw.dispatch(ch.token, {"jsonrpc": "2.0", "id": 11, "method": "tools/call", "params": {"name": "ro.operator_fail", "arguments": {}}})
        op_details = op_resp["result"]["structuredContent"]["details"]
        assert isinstance(op_details.get("audit_ref"), str) and op_details["audit_ref"]
        assert op_details.get("audit_status") != "skipped_read_only"
        assert op_details["recovery_owner"] == "operator"
        assert fw_count >= 1

        for code in (
            "quest_goal_root_unauthorized",
            "quest_research_time_root_unauthorized",
            "root_agent_human_request_effect_unauthorized",
            "root_agent_acquisition_effect_unauthorized",
            "deepfetch_source_action_unauthorized",
        ):
            contract = error_contract_from_legacy(code, effect=True)
            assert contract.effect_status.value == "rejected", code
            assert contract.recovery_owner.value == "agent", code
            assert contract.next_action.value == "correct_input", code
            assert contract.retry_allowed is False, code
            assert contract.reconcile_same_effect is False, code
            assert contract.feedback_stage_agent is True, code

        _, eff_resp = gw.dispatch(
            ch.token, {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "eff.unauth", "arguments": {"effect_id": "e1"}}}
        )
        eff_details = eff_resp["result"]["structuredContent"]["details"]
        assert eff_details["effect_status"] == "rejected"
        assert eff_details["recovery_owner"] == "agent"
        assert eff_details["next_action"] == "correct_input"
        assert eff_details["retry_allowed"] is False
        assert eff_details["reconcile_same_effect"] is False
        assert eff_details["feedback_stage_agent"] is True
        internal = error_contract_from_legacy("idea_runtime_binding_unauthorized", effect=True)
        assert internal.recovery_owner.value != "agent"
    finally:
        db.close()


def test_first_turn_include_direction_and_turn2_delta_only_goal_propagation(
    tmp_path: Path,
) -> None:
    goal_1 = {
        "kind": "QuestGoalRevision",
        "quest_ref": "quest:dir",
        "goal_revision_ref": "quest_goal_revision:dir1",
        "goal": {"statement": "Direction v1."},
    }
    goal_2 = {
        "kind": "QuestGoalRevision",
        "quest_ref": "quest:dir",
        "goal_revision_ref": "quest_goal_revision:dir2",
        "goal": {"statement": "Direction v2."},
    }
    cut_1 = FrozenGuidanceCut(
        binding=FrozenGuidanceBinding(GuidanceOperationIdentity("idea", "r1", "o1"), "quest:dir", "snap:1", "a" * 64),
        provenance_ref="prov:1",
        deliveries=(),
        direction_cut={"goal": goal_1, "conditions": {"enduring": [], "runtime_conditions": {"revision": 1}}},
    )
    cut_2 = replace(
        cut_1,
        direction_cut={"goal": goal_2, "conditions": {"enduring": [], "runtime_conditions": {"revision": 1}}},
    )
    p1 = guidance_prompt(cut_1, resident=True, include_body=False, include_direction=True, previous_prompts=())
    assert "current_goal_and_conditions=" in p1 and "Direction v1." in p1
    p2 = guidance_prompt(cut_1, resident=True, include_body=False, include_direction=False, previous_prompts=(p1,))
    assert "current_goal_and_conditions=" not in p2
    p2_evolved = guidance_prompt(cut_2, resident=True, include_body=False, include_direction=False, previous_prompts=(p1,))
    assert "current_goal_and_conditions=" in p2_evolved and "Direction v2." in p2_evolved
    p3 = guidance_prompt(cut_2, resident=True, include_body=False, include_direction=False, previous_prompts=(p1, p2_evolved))
    assert "current_goal_and_conditions=" not in p3
    p_revert = guidance_prompt(cut_1, resident=True, include_body=False, include_direction=False, previous_prompts=(p1, p2_evolved))
    assert "current_goal_and_conditions=" in p_revert and "Direction v1." in p_revert

    class _CutAuthority:
        def freeze_operation_guidance(self, _op):
            return cut_1

        def query_pending_human_responses(self, _scope):
            return ()

        def human_response_events(self, *, scope=None):
            return ()

        def verify_frozen_guidance_binding(self, _binding):
            return None

        def recover_operation_guidance(self, _op):
            return cut_1

    idea_out = idea_fixtures._idea_set()
    idea_runner = idea_fixtures._SequenceRunner([{"outcome": idea_out}, {"outcome": idea_out}])
    idea_adapter = CodexIdeaSkillAdapter(
        tmp_path / "idea-dir", executable=str(plan_fixtures._fake_codex(tmp_path / "idea-codex")), process_runner=idea_runner
    )
    idea_adapter.bind_human_guidance_authority(_CutAuthority())
    idea_req_1 = idea_fixtures._request(runtime_binding=idea_adapter.runtime_binding())
    idea_req_1 = replace(
        idea_req_1,
        run_ref="run:idea-1",
        attempt_ref="att:1",
        fence_ref="fence:1",
        job_ref="job:idea-1",
        context_pack={**idea_req_1.context_pack, "goal_revision_ref": goal_1["goal_revision_ref"], "quest_goal_revision": goal_1},
    )
    idea_req_1 = replace(idea_req_1, context_pack_hash=canonical_hash(idea_req_1.context_pack))
    draft = idea_adapter.generate_draft(idea_req_1)
    assert "current_goal_and_conditions=" in idea_runner.calls[0][1]
    idea_adapter.generate_draft(replace(
        idea_req_1,
        attempt_ref="att:2", fence_ref="fence:2", job_ref="job:idea-2", native_session_ref=draft.primary_session_ref,
        predecessor_submission_ref="s1", owner_rejection_receipt_ref="r1", owner_rejection_kind="domain",
        owner_feedback=("Revise.",),
    ))
    assert "current_goal_and_conditions=" not in idea_runner.calls[1][1]

    plan_doc = plan_fixtures._plan()
    plan_runner = plan_fixtures._SequenceRunner([{"plan": deepcopy(plan_doc)}, {"plan": deepcopy(plan_doc)}])
    plan_adapter = CodexPlanSkillAdapter(
        tmp_path / "plan-dir", executable=str(plan_fixtures._fake_codex(tmp_path / "plan-codex")), process_runner=plan_runner
    )
    plan_adapter.bind_human_guidance_authority(_CutAuthority())
    plan_req_1 = plan_fixtures._request(runtime_binding=plan_adapter.runtime_binding())
    plan_req_1 = replace(
        plan_req_1,
        run_ref="run:plan-1",
        attempt_ref="att:1",
        fence_ref="fence:1",
        job_ref="job:plan-1",
        context_pack={**plan_req_1.context_pack, "goal_revision_ref": goal_1["goal_revision_ref"], "quest_goal_revision": goal_1},
    )
    plan_req_1 = replace(plan_req_1, context_pack_hash=canonical_hash(plan_req_1.context_pack))
    p_draft = plan_adapter.generate_draft(plan_req_1)
    assert "current_goal_and_conditions=" in plan_runner.calls[0][1]

    rt, _owner, codex = _runtime_with_guidance(tmp_path / "target-dir", _CutAuthority())
    rt.run_or_resume_target_root("target-harness-request:test", prompt="Run target turn 1", mcp_base_url="http://127.0.0.1:8765")
    assert "current_goal_and_conditions=" in codex.invocations[0].prompt
    rt.run_or_resume_target_root("target-harness-request:test", prompt="Continue turn 2", mcp_base_url="http://127.0.0.1:8765")
    assert "current_goal_and_conditions=" not in codex.invocations[1].prompt


def _runtime_with_guidance(tmp_path: Path, guidance_authority):
    from meta_research.harness import HarnessRuntime
    from test_harness_target_root import (
        _Gateway, _Owner, _ResidentScopeVerifier, _WorkspaceResolver, _target_request,
    )

    class _GuidanceOwner(_Owner):
        def prior_target_inputs(self, run_ref: str) -> tuple[str, ...]:
            return tuple(
                op.frozen_prompt
                for op in reversed(self.operations)
                if op.run_ref == run_ref
                and op.status in {"completed", "executed"}
                and op.frozen_prompt is not None
            )

    class _GuidanceGateway(_Gateway):
        def issue_channel(self, *, guidance_binding=None, **kwargs):
            del guidance_binding
            return super().issue_channel(**kwargs)

    class _GuidanceScopeVerifier(_ResidentScopeVerifier):
        def verify_guidance_read_scope(self, *_args, **_kwargs) -> None:
            return None

    tmp_path.mkdir(parents=True, exist_ok=True)
    request = _target_request()
    owner = _GuidanceOwner(request)
    codex = _TargetRootAdapter("codex")
    rt = HarnessRuntime(owner, _GuidanceGateway(), (codex,))
    rt.bind_target_workspace_resolver(_WorkspaceResolver(tmp_path))
    rt.bind_resident_mcp_scope_verifier(_GuidanceScopeVerifier())
    rt.bind_human_guidance_authority(guidance_authority)
    return rt, owner, codex


def test_goal_evolution_g1_to_g2_propagates_to_bundle_and_subsequent_targets(
    tmp_path: Path,
) -> None:
    runtime = _bundle_runtime(tmp_path / "g1-to-g2-e2e", plan_skill_provider=_TwoGapPlanSkill())
    try:
        graph, target1, bundle_run, dispatch1, launch_req1 = _ready_launch(runtime)
        quest_ref = graph.quest_ref
        g1 = runtime.owners.research_graph.query_current_quest_goal_revision(quest_ref)
        assert g1 is not None

        ack1 = runtime.owners.agent_runtime.admit_target_launch(
            launch_req1, dispatch_decision_ref=dispatch1.decision_ref, idempotency_key="g1-launch-t1"
        )
        t1_ctx_before = runtime.target_run_authorities.research_graph.query_target_research_context(target_ref=target1.target_ref)
        assert t1_ctx_before["goal_revision_ref"] == g1["goal_revision_ref"]

        intake = runtime.owners.research_memory.submit_asset_intake(
            AssetIntakeRequest(
                source_kind="text", custody_mode="managed", display_name="g2-evidence.txt",
                media_type="text/plain", content=b"New evidence shifts direction to G2.\n", origin_quest_ref=quest_ref,
            ),
            idempotency_key="g1-to-g2-ev",
        ).asset
        runtime.owners.research_graph.accept_asset_role(
            binding=intake.as_binding(), role="evidence", quest_ref=quest_ref, idempotency_key="g1-to-g2-role"
        )
        author = ResearchRoot(
            kind="bundle",
            run_ref=bundle_run.run_ref,
            attempt_ref=bundle_run.attempt_ref,
            root_session_ref=bundle_run.root_session_ref,
            fence_ref=bundle_run.fence_ref,
            runtime_binding_hash=bundle_run.runtime_binding_hash,
            operation_ref="op-evolve-g2",
        )
        with runtime._database.fenced_write() as conn:
            dcut = runtime.owners.research_graph.freeze_quest_direction(connection=conn, quest_ref=quest_ref, author=author)
        conds = dcut["conditions"]
        works = tuple(WorkHandle(**item) for item in dcut["work"]["items"])
        decisions = []
        for w in works:
            if w.kind == "target":
                decisions.append(WorkDecision(kind="stop", work=w, reason="Stop t1 or keep.", retention={"kind": "inspected_none", "workspace_ref": w.workspace_ref}) if w.workspace_ref else WorkDecision(kind="do_not_start", work=w, reason="Hold."))
            else:
                decisions.append(WorkDecision(kind="continue", work=w, reason="Keep Bundle active under G2."))
        evo = EvolutionDecision(
            expected_revision=dcut["goal"]["goal_revision_ref"],
            conditions_basis=conds["basis_ref"],
            work_basis=dcut["work"]["basis_ref"],
            cause={"kind": "evidence", "evidence": [{"source_ref": intake.asset_ref, "version_ref": intake.version_ref}]},
            replacement={"goal": "G2 evolved direction.", "completion_criteria": "G2 verified."},
            criteria_review="Shifted from G1 to G2.",
            judgment="New evidence favors G2.",
            conditions={
                "runtime_conditions_ref": conds["runtime_conditions"]["revision"],
                "assessments": [{"condition_ref": c["condition_ref"], "disposition": "preserved", "explanation": "Keep."} for c in conds["enduring"]],
                "newly_identified": [],
            },
            arrangements=tuple(decisions),
            following_direction="Dispatch G2 targets.",
        )
        evolved = runtime.owners.research_graph.evolve_quest_goal(
            author=author,
            quest_ref=quest_ref,
            direction_cut=dcut,
            effect_id="evolve-to-g2",
            decision=evo,
        )
        g2 = evolved["goal"]
        assert g2["goal_revision_ref"] != g1["goal_revision_ref"]

        t1_ctx_after = runtime.target_run_authorities.research_graph.query_target_research_context(target_ref=target1.target_ref)
        assert t1_ctx_after["goal_revision_ref"] == g1["goal_revision_ref"]

        plan_req = runtime.owners.advancement_engine.query_bundle_stage_request(graph.cycle_ref)
        second_key = str(plan_req.context_pack["accepted_formal_plan_binding"]["plan_document"]["experiment_briefs"][1]["experiment_key"])
        t2_candidate = _formal_candidate(
            completion_document=graph.target_plan["completion_contract"],
            label="target-after-g2",
            experiment_key=second_key,
            cell=f"measurement:{second_key}",
            depends_on=(),
        )
        from meta_research.bundle_target_contract import FORMAL_STRATEGY_UPDATE_SCHEMA_REF
        strategy_update = {
            "schema_ref": FORMAL_STRATEGY_UPDATE_SCHEMA_REF,
            "revision": graph.head_generation + 2,
            "candidates": [t2_candidate],
            "requires_accepted_labels": [],
            "strategy_complete": False,
        }
        inbox_checkpoint = runtime.owners.agent_runtime.query_bundle_inbox_checkpoint(bundle_run.run_ref)
        assert inbox_checkpoint is not None
        proposal = runtime.owners.agent_runtime.record_bundle_target_proposal(
            run_ref=bundle_run.run_ref,
            attempt_ref=bundle_run.attempt_ref,
            fence_ref=bundle_run.fence_ref,
            native_session_ref=bundle_run.native_session_ref,
            graph_ref=graph.graph_ref,
            base_generation=graph.head_generation,
            base_head_receipt=graph.head_receipt,
            strategy_update=strategy_update,
            inbox_checkpoint=inbox_checkpoint,
            idempotency_key="append-t2-after-g2",
        )
        appended = runtime.owners.research_graph.append_target_batch(
            graph_ref=graph.graph_ref,
            proposal_ref=proposal.proposal_ref,
            proposal=proposal.proposal,
            proposal_hash=proposal.proposal_hash,
            proposal_receipt=proposal.receipt,
        )
        updated_graph = runtime.owners.research_graph.query_target_graph(graph.request_ref)
        assert updated_graph is not None and updated_graph.head_generation == appended.generation
        target2 = updated_graph.targets[-1]
        t2_ctx = runtime.target_run_authorities.research_graph.query_target_research_context(target_ref=target2.target_ref)
        assert t2_ctx["goal_revision_ref"] == g2["goal_revision_ref"]
        assert t2_ctx["quest_goal_revision"]["goal_revision_ref"] == g2["goal_revision_ref"]

        observed_requests = []
        from test_public_bundle_stage import _DeterministicBundleSkill

        class _CapturingBundle(_DeterministicBundleSkill):
            def propose_target_batch(self, request):
                observed_requests.append(("batch", request.quest_goal_revision))
                return super().propose_target_batch(request)

            def schedule_targets(self, request):
                observed_requests.append(("dispatch", request.quest_goal_revision))
                return super().schedule_targets(request)

        capturing = _CapturingBundle()
        auth = getattr(runtime.bundle_stage._provider, "_full_conformance_authority", None)
        if auth is not None:
            capturing.bind_full_conformance_authority(auth)
        runtime.bundle_stage._provider = capturing
        runtime.bundle_stage._provider_factory = None
        runtime.bundle_stage._input_error_recheck_at.clear()
        for _ in range(8):
            if not runtime.bundle_stage.process_once():
                break
            if any(rev and rev["goal_revision_ref"] == g2["goal_revision_ref"] for _, rev in observed_requests):
                break
        assert any(rev and rev["goal_revision_ref"] == g2["goal_revision_ref"] for _, rev in observed_requests), observed_requests

        live_goal = runtime.bundle_stage._live_quest_goal_revision(
            SimpleNamespace(
                accepted_question=SimpleNamespace(quest_ref=quest_ref),
                context_pack={"quest_goal_revision": g1},
            )
        )
        assert live_goal is not None and live_goal["goal_revision_ref"] == g2["goal_revision_ref"]
        orig_query_goal = runtime.owners.research_graph.query_current_quest_goal_revision
        try:
            runtime.owners.research_graph.query_current_quest_goal_revision = lambda _q: (_ for _ in ()).throw(
                OwnerConflict("quest_goal_head_missing")
            )
            fallback_goal = runtime.bundle_stage._live_quest_goal_revision(
                SimpleNamespace(
                    accepted_question=SimpleNamespace(quest_ref=quest_ref),
                    context_pack={"quest_goal_revision": g1},
                )
            )
            assert fallback_goal == g1
        finally:
            runtime.owners.research_graph.query_current_quest_goal_revision = orig_query_goal

        (tmp_path / "drift-g2").mkdir(parents=True, exist_ok=True)
        adapter, _runner = bundle_drift_fixtures._adapter(tmp_path / "drift-g2", bundle_drift_fixtures._output("batch"))
        b_req1 = replace(bundle_drift_fixtures._request(adapter, "batch"), quest_goal_revision=g1)
        adapter.propose_target_batch(b_req1)
        from meta_research.idea_skill import IdeaSkillUnavailable
        with pytest.raises(IdeaSkillUnavailable) as batch_err:
            recover_rejected_target_batch(adapter, replace(b_req1, quest_goal_revision=g2), "target-batch-1")
        assert batch_err.value.code == "bundle_review_result_contract_invalid"
        assert batch_err.value.recovery_checkpoint["contract_failure_detail_code"] == "bundle_operation_inputs_changed"

        (tmp_path / "drift-g2-d").mkdir(parents=True, exist_ok=True)
        adapter_d, _runner_d = bundle_drift_fixtures._adapter(tmp_path / "drift-g2-d", bundle_drift_fixtures._output("dispatch"))
        d_req1 = replace(bundle_drift_fixtures._request(adapter_d, "dispatch"), quest_goal_revision=g1)
        adapter_d.schedule_targets(d_req1)
        with pytest.raises(IdeaSkillUnavailable) as disp_err:
            recover_rejected_dispatch(adapter_d, replace(d_req1, quest_goal_revision=g2), "dispatch-1")
        assert disp_err.value.code == "bundle_review_result_contract_invalid"
        assert disp_err.value.recovery_checkpoint["contract_failure_detail_code"] == "bundle_operation_inputs_changed"
    finally:
        runtime.close()


def test_all_five_roots_concise_cycle_position_and_target_continuation_slimming(
    tmp_path: Path,
) -> None:
    pos = {"cycle_ref": "cycle:7", "round": 3, "cycle_focus": "核查跨设备分布", "focus_source": {"outcome_ref": "out:1"}}
    for st, label in (("idea", "Idea"), ("plan", "Plan"), ("bundle", "Bundle"), ("reasoning", "Reasoning")):
        req = SimpleNamespace(context_pack={"cycle_ref": "cycle:7", "cycle_position": pos})
        line = stage_position_prompt(req, st)
        assert line.startswith(f"当前你处于第 3 轮 Cycle（cycle:7）的 {label} 阶段。当前 cycle focus 是：‘核查跨设备分布’。")
        assert "cycle_position={" not in line

    req_init = SimpleNamespace(context_pack={"cycle_ref": "cycle:1", "cycle_position": {"cycle_ref": "cycle:1", "round": None, "cycle_focus": "  ", "focus_source": {"k": 1}}})
    init_line = stage_position_prompt(req_init, "idea")
    assert "cycle:1" in init_line and "尚未形成" in init_line

    idea_runner = idea_fixtures._SequenceRunner([{"outcome": idea_fixtures._idea_set()}, {"outcome": idea_fixtures._idea_set()}])
    idea_ad = CodexIdeaSkillAdapter(tmp_path / "pos-idea", executable=str(plan_fixtures._fake_codex(tmp_path / "ic")), process_runner=idea_runner)
    ireq = idea_fixtures._request(runtime_binding=idea_ad.runtime_binding())
    ireq = replace(ireq, context_pack={**ireq.context_pack, "cycle_ref": "cycle:7", "cycle_position": pos})
    ireq = replace(ireq, context_pack_hash=canonical_hash(ireq.context_pack))
    idraft = idea_ad.generate_draft(ireq)
    idea_ad.generate_draft(replace(ireq, attempt_ref="a2", fence_ref="f2", native_session_ref=idraft.primary_session_ref, owner_feedback=("Fix.",), predecessor_submission_ref="s1", owner_rejection_receipt_ref="r1", owner_rejection_kind="domain"))
    assert "当前你处于第 3 轮 Cycle（cycle:7）的 Idea 阶段" in idea_runner.calls[0][1]
    assert "当前你处于第 3 轮 Cycle（cycle:7）的 Idea 阶段" in idea_runner.calls[1][1]

    plan_runner = plan_fixtures._SequenceRunner([{"plan": plan_fixtures._plan()}, {"plan": plan_fixtures._plan()}])
    plan_ad = CodexPlanSkillAdapter(tmp_path / "pos-plan", executable=str(plan_fixtures._fake_codex(tmp_path / "pc")), process_runner=plan_runner)
    preq = plan_fixtures._request(runtime_binding=plan_ad.runtime_binding())
    preq = replace(preq, context_pack={**preq.context_pack, "cycle_ref": "cycle:7", "cycle_position": pos})
    preq = replace(preq, context_pack_hash=canonical_hash(preq.context_pack))
    pdraft = plan_ad.generate_draft(preq)
    plan_ad.generate_draft(replace(preq, attempt_ref="a2", fence_ref="f2", native_session_ref=pdraft.primary_session_ref, owner_feedback=("Fix.",), predecessor_submission_ref="s1", owner_rejection_receipt_ref="r1", owner_rejection_kind="domain"))
    assert "当前你处于第 3 轮 Cycle（cycle:7）的 Plan 阶段" in plan_runner.calls[0][1]
    assert "当前你处于第 3 轮 Cycle（cycle:7）的 Plan 阶段" in plan_runner.calls[1][1]

    (tmp_path / "pos-bundle").mkdir(parents=True, exist_ok=True)
    bad, brunner = bundle_drift_fixtures._adapter(tmp_path / "pos-bundle", bundle_drift_fixtures._output("dispatch"))
    dreq = replace(bundle_drift_fixtures._request(bad, "dispatch"), cycle_ref="cycle:7", cycle_position=pos)
    bad.schedule_targets(dreq)
    assert "当前你处于第 3 轮 Cycle（cycle:7）的 Bundle 阶段" in brunner.calls[0][1]

    rr = reasoning_fixtures._SequenceRunner([reasoning_fixtures._stage_output(), reasoning_fixtures._stage_output()])
    rad = CodexReasoningSkillAdapter(tmp_path / "pos-r", executable=str(reasoning_fixtures._fake_codex(tmp_path / "rc")), process_runner=rr)
    rad.bind_full_conformance_authority(reasoning_fixtures._FullConformanceAuthority())
    rad.configure_resident_mcp_endpoint("http://127.0.0.1:8765")
    rreq = replace(reasoning_fixtures._request(), runtime_binding=rad.runtime_binding(), job_ref="reasoning-pos-turn-1")
    rpos = {**pos, "cycle_ref": rreq.cycle_ref}
    rreq = replace(rreq, context_pack={**rreq.context_pack, "cycle_position": rpos})
    rreq = replace(rreq, context_pack_hash=canonical_hash(rreq.context_pack))
    rdraft = rad.generate_draft(rreq)
    rad.generate_draft(replace(rreq, attempt_ref="a2", fence_ref="f2", job_ref="reasoning-pos-turn-2", native_session_ref=rdraft.primary_session_ref, owner_feedback=("Fix.",), predecessor_candidate_ref="s1", owner_rejection_receipt_ref="r1", owner_rejection_kind="domain"))
    assert f"当前你处于第 3 轮 Cycle（{rreq.cycle_ref}）的 Reasoning 阶段" in rr.calls[0][1]
    assert f"当前你处于第 3 轮 Cycle（{rreq.cycle_ref}）的 Reasoning 阶段" in rr.calls[1][1]

    from meta_research.bundle_protocol import TargetWorkHandle
    handle = TargetWorkHandle("target:9", "trun:9", "rs:9", "att:9", "fence:9", "ib:9", AcceptanceReceipt("rg", "input_binding", "rcpt:i", "ib:9", "a" * 64), (), (), True)
    t_cont = TargetRunRuntime._continuation_prompt(handle=handle, cycle_position=pos, revision_request={"code": "x", "feedback": "Fix"})
    assert "当前你处于第 3 轮 Cycle（cycle:7）的 Target 阶段" in t_cont
    assert "第 3 轮 Cycle（cycle:7）" in t_cont
    assert "核查跨设备分布" in t_cont
    assert "tools.catalog.page(view=overview)" not in t_cont

    huge_note = {f"field_{k}": "x" * 1500 for k in range(8)}
    huge_pack = {
        "cycle_ref": "cycle:7",
        "cycle_position": pos,
        "accepted_question_binding": {"question_ref": "q:1", "content_ref": "c:1", "content_hash": "a" * 64},
        "upstream_stage_closure": [
            {"stage": "plan", "commit_ref": f"c:{i}", "closure": {"research_notes": [dict(huge_note), dict(huge_note)]}}
            for i in range(3)
        ],
    }
    view = stage_context_view("idea", huge_pack, context_pack_ref="cp:1", context_pack_hash="b" * 64)
    assert view["section_delivery"].get("current_handoff_notes") == "omitted"
    omitted_notes = view.get("current_handoff_notes")
    assert isinstance(omitted_notes, list) and len(omitted_notes) == 6
    assert all(
        isinstance(item, dict)
        and item.get("omitted_from_preview") is True
        and isinstance(item.get("reader"), dict)
        and item["reader"].get("operation") == "research_memory.stage_context.read"
        and "research_note" not in item
        and "notes" not in item
        and "scientific_summary" not in item
        for item in omitted_notes
    )


def test_work_materials_single_file_cross_turn_and_native_read_feedback(
    tmp_path: Path,
) -> None:
    src_human = tmp_path / "human-single.txt"
    src_human.write_text("Human single file content.\n", encoding="utf-8")
    src_acquired = tmp_path / "agent-acquired.txt"
    src_acquired.write_text("Agent acquired content.\n", encoding="utf-8")
    src_native = tmp_path / "native-readable.txt"
    src_native.write_text("Read via native shell/file tools.\n", encoding="utf-8")

    rt = _make_material_runtime(tmp_path / "wm-gap")
    try:
        scope, ctx1, opened = _open(rt)
        qref = scope["quest_ref"]
        ch1 = _channel(rt, ctx1)
        from fastapi.testclient import TestClient
        from meta_research.web import create_app
        src_missing = tmp_path / "gone.txt"
        src_missing.write_text("Gone later.\n", encoding="utf-8")
        with TestClient(create_app(rt, base_url="http://testserver", control_key="control")) as client:
            headers = _login(client, rt)
            sel_human = client.get("/api/v1/server-materials/inspect", params={"path": str(src_human)}).json()
            sel_native = client.get("/api/v1/server-materials/inspect", params={"path": str(src_native)}).json()
            sel_missing = client.get("/api/v1/server-materials/inspect", params={"path": str(src_missing)}).json()
            resp = client.post(
                f"/api/v1/human-requests/{opened['request_ref']}/responses",
                headers=headers,
                json={
                    "decision": "provided",
                    "note": "Three single files.",
                    "facts": {},
                    "materials": [
                        {"kind": "server_reference", "selection": sel_human},
                        {"kind": "server_reference", "selection": sel_native},
                        {"kind": "server_reference", "selection": sel_missing},
                    ],
                },
            )
            assert resp.status_code == 201
            ref_human, ref_native, ref_missing = [
                item["reference_ref"] for item in resp.json()["delivery"]["work_materials"]
            ]

        acq = _accepted(_call(rt, ch1, "research_workspace.materials.acquire", effect_id="acq-1", absolute_path=str(src_acquired), description="Agent source."))
        ref_acq = acq["references"][0]["reference_ref"]

        adopted_native = _accepted(_call(rt, ch1, "research_workspace.materials.feedback", **_feedback(ref_native, effect_id="fb-native", disposition="adopted")))
        assert adopted_native["disposition"] == "adopted"
        assert rt.owners.human_collaboration.query_work_material(ref_native)["read_state"] == "not_read"

        successor = _external_context(_scope(rt, quest_ref=qref, run_ref="later-cycle-root", root_ref="later-cycle-session"))
        ch2 = _channel(rt, successor)
        discovered = _accepted(_call(rt, ch2, "research_workspace.materials.discover"))
        refs = {item["reference_ref"] for item in discovered["references"]}
        assert ref_human in refs
        assert ref_acq not in refs
        page = _accepted(_call(rt, ch2, "research_workspace.materials.discover", reference_ref=ref_human))
        read_res = _accepted(_call(
            rt, ch2, "research_workspace.materials.read",
            reference_ref=ref_human, path="", observation_ref=page["entries"][0]["observation"]["observation_ref"],
        ))
        assert read_res["text"] == "Human single file content.\n"

        src_missing.unlink()
        rej = _call(rt, ch1, "research_workspace.materials.feedback", **_feedback(ref_missing, effect_id="fb-missing", disposition="adopted"))
        assert rej.get("isError") and "material_root_read_required" in str(rej)
    finally:
        rt.close()


def test_acquisition_root_targetless_preflight_and_oa_then_institution_policy(
    tmp_path: Path,
) -> None:
    runner = _AcqSequenceRunner([{
        "status": "unavailable",
        "browser_context_ref": None,
        "reason_code": "no_paper_targets",
        "evidence": {
            "configuration_health": "ok",
            "browser_control": "skipped",
            "authorized_resource": "no_targets",
            "extra_diagnostic": "allowed",
        },
    }])
    adapter = CodexAcquisitionRootAdapter(
        tmp_path / "acq-synonym", _AcquisitionDelegate(), executable=str(_acq_fake_codex(tmp_path / "codex")), process_runner=runner
    )
    res = adapter.preflight(AcquisitionPreflightRequest("s1", "init1", 1, "a" * 64, "oa_only", "", str(tmp_path / "priv")))
    assert res.status == "ready"
    assert "oa_then_institution" in runner.prompts[0]
    assert "preflight" in runner.prompts[0] and (
        "无需论文列表" in runner.prompts[0] or "无论文列表" in runner.prompts[0] or "目标论文" in runner.prompts[0]
    )

    runner_inst_ok = _AcqSequenceRunner([{
        "status": "unavailable",
        "browser_context_ref": "ctx-verified-1",
        "reason_code": "missing_paper_targets",
        "evidence": {"configuration_health": "ok", "browser_control": "connected", "authorized_resource": "verified"},
    }])
    adapter_ok = CodexAcquisitionRootAdapter(
        tmp_path / "acq-inst-ok", _AcquisitionDelegate(), executable=str(_acq_fake_codex(tmp_path / "codex-ok")), process_runner=runner_inst_ok
    )
    res_ok = adapter_ok.preflight(
        AcquisitionPreflightRequest("s2", "init2", 1, "a" * 64, "oa_then_institution", "https://lib.example", str(tmp_path / "priv2"))
    )
    assert res_ok.status == "ready" and res_ok.browser_context_ref == "ctx-verified-1"

    runner_inst_bad = _AcqSequenceRunner([{
        "status": "unavailable",
        "browser_context_ref": None,
        "reason_code": "missing_paper_targets",
        "evidence": {"configuration_health": "ok", "browser_control": "not_connected", "authorized_resource": "unverified"},
    }])
    adapter_bad = CodexAcquisitionRootAdapter(
        tmp_path / "acq-inst-bad", _AcquisitionDelegate(), executable=str(_acq_fake_codex(tmp_path / "codex-bad")), process_runner=runner_inst_bad
    )
    res_bad = adapter_bad.preflight(
        AcquisitionPreflightRequest("s3", "init3", 1, "a" * 64, "oa_then_institution", "https://lib.example", str(tmp_path / "priv3"))
    )
    assert res_bad.status == "unavailable" and res_bad.browser_context_ref is None

    runner_site_missing = _AcqSequenceRunner([{
        "status": "unavailable",
        "browser_context_ref": None,
        "reason_code": "target_site_missing",
        "evidence": {"configuration_health": "ok", "browser_control": "skipped", "authorized_resource": "no_targets"},
    }])
    adapter_site_missing = CodexAcquisitionRootAdapter(
        tmp_path / "acq-site-missing", _AcquisitionDelegate(), executable=str(_acq_fake_codex(tmp_path / "codex-site")), process_runner=runner_site_missing
    )
    res_site_missing = adapter_site_missing.preflight(
        AcquisitionPreflightRequest("s4", "init4", 1, "a" * 64, "oa_only", "", str(tmp_path / "priv4"))
    )
    assert res_site_missing.status == "unavailable" and res_site_missing.reason_code == "target_site_missing"

    runner_broken_health = _AcqSequenceRunner([{
        "status": "unavailable",
        "browser_context_ref": "ctx-verified-2",
        "reason_code": "missing_paper_targets",
        "evidence": {"configuration_health": "broken", "browser_control": "connected", "authorized_resource": "verified"},
    }])
    adapter_broken_health = CodexAcquisitionRootAdapter(
        tmp_path / "acq-broken-health", _AcquisitionDelegate(), executable=str(_acq_fake_codex(tmp_path / "codex-broken")), process_runner=runner_broken_health
    )
    res_broken_health = adapter_broken_health.preflight(
        AcquisitionPreflightRequest("s5", "init5", 1, "a" * 64, "oa_then_institution", "https://lib.example", str(tmp_path / "priv5"))
    )
    assert res_broken_health.status == "unavailable" and res_broken_health.reason_code == "missing_paper_targets"

    runner_inst_missing = _AcqSequenceRunner([{
        "status": "unavailable",
        "browser_context_ref": "ctx-verified-3",
        "reason_code": "institution_targets_missing",
        "evidence": {"configuration_health": "ok", "browser_control": "connected", "authorized_resource": "verified"},
    }])
    adapter_inst_missing = CodexAcquisitionRootAdapter(
        tmp_path / "acq-inst-missing", _AcquisitionDelegate(), executable=str(_acq_fake_codex(tmp_path / "codex-im")), process_runner=runner_inst_missing
    )
    res_inst_missing = adapter_inst_missing.preflight(
        AcquisitionPreflightRequest("s6", "init6", 1, "a" * 64, "oa_then_institution", "https://lib.example", str(tmp_path / "priv6"))
    )
    assert res_inst_missing.status == "unavailable" and res_inst_missing.reason_code == "institution_targets_missing"

    runner_bad_browser = _AcqSequenceRunner([{
        "status": "unavailable",
        "browser_context_ref": "ctx-verified-4",
        "reason_code": "missing_paper_targets",
        "evidence": {"configuration_health": "ok", "browser_control": "not_connected", "authorized_resource": "verified"},
    }])
    adapter_bad_browser = CodexAcquisitionRootAdapter(
        tmp_path / "acq-bad-browser", _AcquisitionDelegate(), executable=str(_acq_fake_codex(tmp_path / "codex-bb")), process_runner=runner_bad_browser
    )
    res_bad_browser = adapter_bad_browser.preflight(
        AcquisitionPreflightRequest("s7", "init7", 1, "a" * 64, "oa_then_institution", "https://lib.example", str(tmp_path / "priv7"))
    )
    assert res_bad_browser.status == "unavailable" and res_bad_browser.browser_context_ref == "ctx-verified-4"
