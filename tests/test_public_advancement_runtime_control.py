from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from meta_research.deepfetch import DeepFetchUnavailable, validate_deepfetch_result
from meta_research.idea_stage import _public_run
from meta_research.idea_skill import IdeaSkillUnavailable
from meta_research.experiment_contract import (
    ExperimentObservation,
    ExperimentProviderUnavailable,
)
from meta_research.owners.common import AcceptanceReceipt, OwnerConflict, canonical_hash
from meta_research.migration import upgrade_database
from meta_research.paths import prepare_data_root
from meta_research.web import create_app
from test_idea_owner_integrity import (
    _admit_direct_idea_request,
    _confirm_question_with_prefix,
    _no_viable_outcome,
    _prepare_direct_idea_request,
    _record_direct_execution,
    _review,
    _runtime_binding,
)
from test_idea_stage_recovery import _IdeaProvider, _runtime
from test_migration_recovery import _upgrade_to_revision
from test_plan_stage_migration import _seed_completed_idea_chain
from test_public_experiment_measurement import (
    ExperimentIntent,
    _DeterministicExperimentProvider,
    _confirm_direct_quest as _confirm_experiment_quest,
    _runtime as _experiment_runtime,
)
from test_public_manual_question_lifecycle import (
    DeterministicManualDeepFetchProvider,
    _accept_root_question,
    _build_runtime as _manual_runtime,
    _confirm_waived_manual_question,
    _open_and_confirm_seed,
)


def _confirmed_control(human, *, scope_ref: str, payload: dict[str, object], key: str):
    drafted = human.create_command_draft(
        scope_ref,
        {"command_kind": "research_control", "payload": payload},
        f"{key}-draft",
    )
    previewed = human.preview_command(
        drafted["intent_id"],
        drafted["draft_revision"],
        drafted["draft_hash"],
        f"{key}-preview",
    )
    preview = previewed["impact_preview"]
    assert preview is not None
    return human.confirm_command(
        drafted["intent_id"],
        drafted["draft_revision"],
        drafted["draft_hash"],
        preview["preview_ref"],
        preview["preview_hash"],
        f"{key}-confirm",
    )


def _execute_control(human, command: dict[str, object], key: str):
    confirmation = command["confirmation_receipt"]
    assert isinstance(confirmation, dict)
    return human.execute_confirmed_command(
        command["intent_id"],
        confirmation["receipt_ref"],
        f"{key}-execute",
    )


def _drop_control_execution_receipt(runtime, intent_id: str) -> None:
    with runtime._database.write() as connection:
        connection.execute(
            text("DELETE FROM hc_command_executions WHERE intent_id = :intent_id"),
            {"intent_id": intent_id},
        )
        connection.execute(
            text(
                "UPDATE human_collaboration_state SET revision = revision - 1, "
                "command_execution_count = command_execution_count - 1 WHERE "
                "singleton = 'owner'"
            )
        )
        connection.execute(
            text(
                "UPDATE hc_control_sagas SET status = CASE WHEN target_scope = 'run' "
                "THEN 'runtime_applied' ELSE 'advancement_applied' END, updated_at = "
                ":now WHERE intent_id = :intent_id"
            ),
            {"intent_id": intent_id, "now": 0.0},
        )


def _authenticated_client(runtime) -> tuple[TestClient, dict[str, str]]:
    base_url = "http://testserver"
    client = TestClient(
        create_app(runtime, base_url=base_url, control_key="control-secret"),
        base_url=base_url,
    )
    bootstrap = runtime.authentication.issue_bootstrap_token()
    response = client.post(
        "/auth/bootstrap",
        headers={"Origin": base_url},
        json={"token": bootstrap},
    )
    assert response.status_code == 200
    return client, {
        "Origin": base_url,
        "X-CSRF-Token": response.json()["csrf_token"],
    }


def _write_headers(auth: dict[str, str], key: str) -> dict[str, str]:
    return {**auth, "Idempotency-Key": key}


def test_pause_and_resume_preserve_run_identity_and_recover_after_restart(
    tmp_path: Path,
) -> None:
    data_root = prepare_data_root(tmp_path / "pause-resume")
    runtime = _runtime(data_root, _IdeaProvider())
    try:
        completed = _confirm_question_with_prefix(runtime, "pause-resume")
        _question, _request, run = _admit_direct_idea_request(
            runtime, completed, "pause-resume"
        )
        human = runtime.owners.human_collaboration
        foreground = runtime.owners.advancement_engine.query_foreground(
            completed["quest_ref"]
        )
        assert foreground is not None

        confirmed = _confirmed_control(
            human,
            scope_ref=f"quest:{completed['quest_ref']}",
            payload={
                "action": "pause",
                "target": {
                    "quest_ref": completed["quest_ref"],
                    "cycle_ref": completed["cycle_ref"],
                    "question_ref": foreground["question_ref"],
                    "epoch": foreground["epoch"],
                },
                "reason": "operator_requested",
            },
            key="pause-resume-pause",
        )
        preview = confirmed["impact_preview"]
        assert preview is not None
        assert [item["source_owner"] for item in preview["owner_previews"]] == [
            "advancement_engine",
            "agent_runtime",
        ]
        # Human confirmation is an authorization fact, not an Owner mutation.
        assert runtime.owners.advancement_engine.query_foreground(
            completed["quest_ref"]
        )["status"] == "active"

        paused = _execute_control(human, confirmed, "pause-resume-pause")
        assert paused["executed"] is True
        assert paused["control_execution"]["status"] == "completed"
        paused_run = runtime.owners.agent_runtime.query_managed_run(run.run_ref)
        assert paused_run is not None
        assert paused_run["status"] == "suspended"
        assert paused_run["attempt_ref"] == run.attempt_ref
        assert paused_run["root_session_ref"] == run.root_session_ref
        assert paused_run["fence_ref"] == run.fence_ref
        assert paused_run["safe_point_ref"]
        with pytest.raises(OwnerConflict, match="runtime_run_suspended"):
            runtime.owners.agent_runtime.record_idea_primary_draft(
                run_ref=run.run_ref,
                attempt_ref=run.attempt_ref,
                fence_ref=run.fence_ref,
                native_session_ref="native-paused",
                runtime_binding=run.runtime_binding,
                draft={"kind": "NoViableCandidate"},
                adapter_kind="test_control",
                idempotency_key="pause-resume-late-draft",
            )
    finally:
        runtime.close()

    restarted = _runtime(data_root, _IdeaProvider())
    try:
        recovered = restarted.owners.agent_runtime.query_managed_run(run.run_ref)
        assert recovered is not None
        assert recovered["status"] == "suspended"
        assert recovered["safe_point_ref"] == paused_run["safe_point_ref"]
        foreground = restarted.owners.advancement_engine.query_foreground(
            completed["quest_ref"]
        )
        assert foreground is not None
        confirmed_resume = _confirmed_control(
            restarted.owners.human_collaboration,
            scope_ref=f"quest:{completed['quest_ref']}",
            payload={
                "action": "resume",
                "target": {
                    "quest_ref": completed["quest_ref"],
                    "cycle_ref": completed["cycle_ref"],
                    "question_ref": foreground["question_ref"],
                    "epoch": foreground["epoch"],
                },
                "reason": "operator_requested",
            },
            key="pause-resume-resume",
        )
        _execute_control(
            restarted.owners.human_collaboration,
            confirmed_resume,
            "pause-resume-resume",
        )
        resumed = restarted.owners.agent_runtime.query_managed_run(run.run_ref)
        assert resumed is not None
        assert resumed["status"] == "running"
        assert resumed["attempt_ref"] == run.attempt_ref
        assert resumed["root_session_ref"] == run.root_session_ref
        assert resumed["fence_ref"] == run.fence_ref
    finally:
        restarted.close()


def test_control_reconciles_owner_effects_after_hc_receipt_is_interrupted(
    tmp_path: Path,
) -> None:
    runtime = _runtime(prepare_data_root(tmp_path / "receipt-recovery"), _IdeaProvider())
    try:
        completed = _confirm_question_with_prefix(runtime, "receipt-recovery")
        _question, _request, _run = _admit_direct_idea_request(
            runtime, completed, "receipt-recovery"
        )
        human = runtime.owners.human_collaboration
        foreground = runtime.owners.advancement_engine.query_foreground(
            completed["quest_ref"]
        )
        assert foreground is not None
        confirmed = _confirmed_control(
            human,
            scope_ref=f"quest:{completed['quest_ref']}",
            payload={
                "action": "pause",
                "target": {
                    "quest_ref": completed["quest_ref"],
                    "cycle_ref": completed["cycle_ref"],
                    "question_ref": foreground["question_ref"],
                    "epoch": foreground["epoch"],
                },
                "reason": "operator_requested",
            },
            key="receipt-recovery",
        )
        executed = _execute_control(human, confirmed, "receipt-recovery-first")
        owner_receipts = executed["control_execution"]["owner_receipts"]

        # This is the durable state left when every Owner committed but the daemon
        # stopped before HC could persist its aggregate execution receipt.
        _drop_control_execution_receipt(runtime, confirmed["intent_id"])

        recovered = _execute_control(human, confirmed, "receipt-recovery-new-browser")
        assert recovered["executed"] is True
        assert recovered["control_execution"]["owner_receipts"] == owner_receipts
    finally:
        runtime.close()


def test_stale_owner_prepare_releases_foreground_without_partial_effect(
    tmp_path: Path,
) -> None:
    runtime = _runtime(prepare_data_root(tmp_path / "prepare-stale"), _IdeaProvider())
    try:
        completed = _confirm_question_with_prefix(runtime, "prepare-stale")
        _question, _request, run = _admit_direct_idea_request(
            runtime, completed, "prepare-stale"
        )
        human = runtime.owners.human_collaboration
        foreground = runtime.owners.advancement_engine.query_foreground(
            completed["quest_ref"]
        )
        assert foreground is not None
        payload = {
            "action": "cancel",
            "target": {
                "quest_ref": completed["quest_ref"],
                "cycle_ref": completed["cycle_ref"],
                "question_ref": foreground["question_ref"],
                "epoch": foreground["epoch"],
            },
            "reason": "operator_requested",
        }
        confirmed = _confirmed_control(
            human,
            scope_ref=f"quest:{completed['quest_ref']}",
            payload=payload,
            key="prepare-stale-old",
        )
        with runtime._database.write() as connection:
            connection.execute(
                text(
                    "UPDATE agent_runtime_state SET revision = revision + 1 WHERE "
                    "singleton = 'owner'"
                )
            )

        with pytest.raises(OwnerConflict, match="command_preview_stale"):
            _execute_control(human, confirmed, "prepare-stale-old")

        unchanged = runtime.owners.advancement_engine.query_foreground(
            completed["quest_ref"]
        )
        assert unchanged is not None
        assert unchanged["status"] == "active"
        assert unchanged["grant_status"] == "active"
        assert unchanged["pending_operation_ref"] is None
        assert runtime.owners.agent_runtime.query_managed_run(run.run_ref)["status"] == (
            "running"
        )

        refreshed = _confirmed_control(
            human,
            scope_ref=f"quest:{completed['quest_ref']}",
            payload=payload,
            key="prepare-stale-refreshed",
        )
        executed = _execute_control(human, refreshed, "prepare-stale-refreshed")
        assert executed["executed"] is True
    finally:
        runtime.close()


def test_graph_apply_drift_compensates_runtime_and_releases_foreground(
    tmp_path: Path,
) -> None:
    runtime = _runtime(prepare_data_root(tmp_path / "graph-apply-drift"), _IdeaProvider())
    try:
        completed = _confirm_question_with_prefix(runtime, "graph-apply-drift")
        root, _request, run = _admit_direct_idea_request(
            runtime, completed, "graph-apply-drift"
        )
        foreground = runtime.owners.advancement_engine.query_foreground(
            completed["quest_ref"]
        )
        assert foreground is not None
        confirmed = _confirmed_control(
            runtime.owners.human_collaboration,
            scope_ref=f"quest:{completed['quest_ref']}",
            payload={
                "action": "prune",
                "target": {
                    "quest_ref": completed["quest_ref"],
                    "cycle_ref": completed["cycle_ref"],
                    "question_ref": root.question_ref,
                    "epoch": foreground["epoch"],
                    "target_question_ref": root.question_ref,
                },
                "reason": "operator_requested",
            },
            key="graph-apply-drift",
        )
        original_apply = runtime.owners.research_graph.apply_question_control

        def apply_after_drift(**values):
            with runtime._database.write() as connection:
                connection.execute(
                    text(
                        "UPDATE rg_graph_heads SET graph_version = graph_version + 1 "
                        "WHERE quest_ref = :quest_ref"
                    ),
                    {"quest_ref": completed["quest_ref"]},
                )
            return original_apply(**values)

        runtime.owners.research_graph.apply_question_control = apply_after_drift
        with pytest.raises(OwnerConflict, match="question_control_reservation_stale"):
            _execute_control(
                runtime.owners.human_collaboration,
                confirmed,
                "graph-apply-drift",
            )

        current = runtime.owners.advancement_engine.query_foreground(
            completed["quest_ref"]
        )
        assert current is not None
        assert current["status"] == "active"
        assert current["pending_operation_ref"] is None
        managed = runtime.owners.agent_runtime.query_managed_run(run.run_ref)
        assert managed is not None
        assert managed["status"] == "running"
        assert managed["root_session_ref"] == run.root_session_ref
        assert managed["attempt_ref"] != run.attempt_ref
        assert runtime.owners.research_graph.query_question_by_ref(
            root.question_ref
        ) is not None
        with runtime._database.read() as connection:
            assert connection.execute(
                text("SELECT COUNT(*) FROM ar_control_compensations")
            ).scalar_one() == 1
            assert connection.execute(
                text(
                    "SELECT status FROM rg_question_control_reservations"
                )
            ).scalar_one() == "aborted"
    finally:
        runtime.close()


def test_compensated_graph_control_finishes_abort_after_daemon_restart(
    tmp_path: Path,
) -> None:
    data_root = prepare_data_root(tmp_path / "compensation-crash-recovery")
    runtime = _runtime(data_root, _IdeaProvider())
    completed = _confirm_question_with_prefix(runtime, "compensation-crash-recovery")
    root, _request, run = _admit_direct_idea_request(
        runtime, completed, "compensation-crash-recovery"
    )
    foreground = runtime.owners.advancement_engine.query_foreground(
        completed["quest_ref"]
    )
    assert foreground is not None
    confirmed = _confirmed_control(
        runtime.owners.human_collaboration,
        scope_ref=f"quest:{completed['quest_ref']}",
        payload={
            "action": "prune",
            "target": {
                "quest_ref": completed["quest_ref"],
                "cycle_ref": completed["cycle_ref"],
                "question_ref": root.question_ref,
                "epoch": foreground["epoch"],
                "target_question_ref": root.question_ref,
            },
            "reason": "operator_requested",
        },
        key="compensation-crash-recovery",
    )
    original_apply = runtime.owners.research_graph.apply_question_control

    def apply_after_drift(**values):
        with runtime._database.write() as connection:
            connection.execute(
                text(
                    "UPDATE rg_graph_heads SET graph_version = graph_version + 1 "
                    "WHERE quest_ref = :quest_ref"
                ),
                {"quest_ref": completed["quest_ref"]},
            )
        return original_apply(**values)

    runtime.owners.research_graph.apply_question_control = apply_after_drift

    def abort_crashes(**_values):
        raise OwnerConflict("simulated_abort_crash")

    runtime.owners.research_graph.abort_question_control = abort_crashes
    with pytest.raises(OwnerConflict, match="simulated_abort_crash"):
        _execute_control(
            runtime.owners.human_collaboration,
            confirmed,
            "compensation-crash-recovery",
        )
    with runtime._database.read() as connection:
        assert connection.execute(
            text(
                "SELECT status FROM hc_control_sagas WHERE intent_id = :intent_id"
            ),
            {"intent_id": confirmed["intent_id"]},
        ).scalar_one() == "compensated"
    compensated = runtime.owners.agent_runtime.query_managed_run(run.run_ref)
    assert compensated is not None
    assert compensated["status"] == "running"
    runtime.close()

    restarted = _runtime(data_root, _IdeaProvider())
    try:
        current = restarted.owners.advancement_engine.query_foreground(
            completed["quest_ref"]
        )
        assert current is not None
        assert current["status"] == "active"
        assert current["pending_operation_ref"] is None
        assert restarted.owners.agent_runtime.query_managed_run(run.run_ref)[
            "status"
        ] == "running"
        assert restarted.owners.research_graph.query_question_by_ref(
            root.question_ref
        ) is not None
        with restarted._database.read() as connection:
            assert connection.execute(
                text(
                    "SELECT status FROM hc_control_sagas WHERE intent_id = :intent_id"
                ),
                {"intent_id": confirmed["intent_id"]},
            ).scalar_one() == "aborted"
        with pytest.raises(OwnerConflict, match="research_control_repreview_required"):
            _execute_control(
                restarted.owners.human_collaboration,
                confirmed,
                "compensation-crash-recovery-new-browser",
            )
    finally:
        restarted.close()


def test_ae_rejects_unverified_runtime_control_receipt(tmp_path: Path) -> None:
    runtime = _runtime(prepare_data_root(tmp_path / "forged-receipt"), _IdeaProvider())
    try:
        completed = _confirm_question_with_prefix(runtime, "forged-receipt")
        foreground = runtime.owners.advancement_engine.query_foreground(
            completed["quest_ref"]
        )
        assert foreground is not None
        payload = {
            "action": "pause",
            "target": {
                "quest_ref": completed["quest_ref"],
                "cycle_ref": completed["cycle_ref"],
                "question_ref": foreground["question_ref"],
                "epoch": foreground["epoch"],
            },
            "reason": "operator_requested",
        }
        _preview, revision = (
            runtime.owners.advancement_engine.preview_foreground_control(payload)
        )
        prepared = runtime.owners.advancement_engine.prepare_foreground_control(
            intent_id="forged-receipt-intent",
            payload=payload,
            expected_revision=revision,
            idempotency_key="forged-receipt-prepare",
        )
        forged = {
            "status": "completed",
            "issuer": "agent_runtime",
            "kind": "runtime_control",
            "operation_ref": prepared["operation_ref"],
            "action": "pause",
            "safe_points": [],
        }
        with pytest.raises(OwnerConflict, match="runtime_control_receipt_invalid"):
            runtime.owners.advancement_engine.complete_foreground_control(
                operation_ref=prepared["operation_ref"],
                runtime_receipt=forged,
                graph_receipt=None,
                idempotency_key="forged-receipt-complete",
            )
        current = runtime.owners.advancement_engine.query_foreground(
            completed["quest_ref"]
        )
        assert current is not None
        assert current["grant_status"] == "active"
    finally:
        runtime.close()


def test_stage_dispositions_distinguish_skipped_from_execution_backed_exhausted(
    tmp_path: Path,
) -> None:
    class _BasisVerifier:
        def verify_stage_disposition_basis(self, **_values) -> None:
            return None

    runtime = _runtime(prepare_data_root(tmp_path / "stage-dispositions"), _IdeaProvider())
    try:
        completed = _confirm_question_with_prefix(runtime, "stage-dispositions")
        engine = runtime.owners.advancement_engine
        engine._stage_disposition_basis_verifier = _BasisVerifier()
        foreground = engine.query_foreground(completed["quest_ref"])
        assert foreground is not None
        for index, (stage, expected_next) in enumerate(
            (("idea", "plan"), ("plan", "bundle"), ("bundle", "reasoning")),
            start=1,
        ):
            assert foreground["stage"] == stage
            basis_ref = f"skip-basis-{stage}"
            basis_receipt = AcceptanceReceipt(
                issuer="test_basis_owner",
                kind="stage_skip_basis",
                receipt_ref=f"skip-receipt-{stage}",
                subject_ref=basis_ref,
                payload_hash=canonical_hash({"basis": basis_ref}),
            )
            committed = engine.commit_stage_disposition(
                cycle_ref=foreground["cycle_ref"],
                stage=stage,
                epoch=foreground["epoch"],
                disposition="skipped",
                basis_kind="test_verified_skip",
                basis_ref=basis_ref,
                basis_receipt=basis_receipt,
                idempotency_key=f"stage-disposition-skip-{index}",
            )
            assert committed.request_ref is None
            assert committed.disposition == "skipped"
            assert engine.commit_stage_disposition(
                cycle_ref=foreground["cycle_ref"],
                stage=stage,
                epoch=foreground["epoch"],
                disposition="skipped",
                basis_kind="test_verified_skip",
                basis_ref=basis_ref,
                basis_receipt=basis_receipt,
                idempotency_key=f"stage-disposition-skip-{index}",
            ) == committed
            foreground = engine.query_foreground(completed["quest_ref"])
            assert foreground is not None
            assert foreground["stage"] == expected_next
        assert engine.query_idea_stage_request(completed["cycle_ref"]) is None
        with pytest.raises(
            OwnerConflict, match="reasoning_stage_disposition_requires_completion"
        ):
            engine.commit_stage_disposition(
                cycle_ref=foreground["cycle_ref"],
                stage="reasoning",
                epoch=foreground["epoch"],
                disposition="skipped",
                basis_kind="test_verified_skip",
                basis_ref="reasoning-skip-basis",
                basis_receipt=AcceptanceReceipt(
                    issuer="test_basis_owner",
                    kind="stage_skip_basis",
                    receipt_ref="reasoning-skip-receipt",
                    subject_ref="reasoning-skip-basis",
                    payload_hash=canonical_hash({"basis": "reasoning-skip-basis"}),
                ),
                idempotency_key="stage-disposition-reasoning-skip",
            )
    finally:
        runtime.close()

    requested_skip_runtime = _runtime(
        prepare_data_root(tmp_path / "stage-requested-skip"), _IdeaProvider()
    )
    try:
        requested_skip = _confirm_question_with_prefix(
            requested_skip_runtime, "stage-requested-skip"
        )
        engine = requested_skip_runtime.owners.advancement_engine
        engine._stage_disposition_basis_verifier = _BasisVerifier()
        _question, request, run = _admit_direct_idea_request(
            requested_skip_runtime,
            requested_skip,
            "stage-requested-skip",
        )
        skip_basis = AcceptanceReceipt(
            issuer="test_basis_owner",
            kind="stage_skip_basis",
            receipt_ref="requested-skip-receipt",
            subject_ref="requested-skip-basis",
            payload_hash=canonical_hash({"basis": "requested-skip-basis"}),
        )
        with pytest.raises(
            OwnerConflict, match="stage_disposition_execution_unexpected"
        ):
            engine.commit_stage_disposition(
                request_ref=request.request_ref,
                disposition="skipped",
                basis_kind="test_verified_skip",
                basis_ref="requested-skip-basis",
                basis_receipt=skip_basis,
                idempotency_key="stage-requested-skip-with-request",
            )
        foreground = engine.query_foreground(requested_skip["quest_ref"])
        assert foreground is not None
        with pytest.raises(
            OwnerConflict, match="stage_disposition_execution_already_started"
        ):
            engine.commit_stage_disposition(
                cycle_ref=foreground["cycle_ref"],
                stage=foreground["stage"],
                epoch=foreground["epoch"],
                disposition="skipped",
                basis_kind="test_verified_skip",
                basis_ref="requested-skip-basis",
                basis_receipt=skip_basis,
                idempotency_key="stage-requested-skip-without-request",
            )
        assert engine.query_foreground(requested_skip["quest_ref"])["stage"] == "idea"
        assert requested_skip_runtime.owners.agent_runtime.query_managed_run(
            run.run_ref
        )["status"] == "running"
    finally:
        requested_skip_runtime.close()

    exhausted_runtime = _runtime(
        prepare_data_root(tmp_path / "stage-exhausted"), _IdeaProvider()
    )
    try:
        exhausted = _confirm_question_with_prefix(exhausted_runtime, "stage-exhausted")
        engine = exhausted_runtime.owners.advancement_engine
        engine._stage_disposition_basis_verifier = _BasisVerifier()
        foreground = engine.query_foreground(exhausted["quest_ref"])
        assert foreground is not None
        basis_receipt = AcceptanceReceipt(
            issuer="test_basis_owner",
            kind="stage_exhaustion_basis",
            receipt_ref="idea-exhausted-receipt",
            subject_ref="idea-exhausted-basis",
            payload_hash=canonical_hash({"basis": "idea-exhausted-basis"}),
        )
        with pytest.raises(
            OwnerConflict, match="stage_disposition_execution_required"
        ):
            engine.commit_stage_disposition(
                cycle_ref=foreground["cycle_ref"],
                stage="idea",
                epoch=foreground["epoch"],
                disposition="exhausted",
                basis_kind="test_verified_exhaustion",
                basis_ref="idea-exhausted-basis",
                basis_receipt=basis_receipt,
                idempotency_key="stage-disposition-exhausted-without-run",
            )

        _question, request, run = _admit_direct_idea_request(
            exhausted_runtime,
            exhausted,
            "stage-exhausted",
        )
        completion_receipt = AcceptanceReceipt(
            issuer="agent_runtime",
            kind="run_execution_completed",
            receipt_ref="idea-exhausted-run-completion",
            subject_ref=run.run_ref,
            payload_hash=canonical_hash(
                {
                    "request_ref": request.request_ref,
                    "run_ref": run.run_ref,
                    "basis_ref": "idea-exhausted-basis",
                }
            ),
        )

        class _RunCompletionVerifier:
            def verify_run_completion_receipt(self, **values) -> None:
                assert values == {
                    "request_ref": request.request_ref,
                    "run_ref": run.run_ref,
                    "attempt_ref": None,
                    "outcome_ref": "idea-exhausted-basis",
                    "receipt": completion_receipt,
                }

        engine._run_completion_verifier = _RunCompletionVerifier()
        committed = engine.commit_stage_disposition(
            request_ref=request.request_ref,
            run_ref=run.run_ref,
            run_completion_receipt=completion_receipt,
            disposition="exhausted",
            basis_kind="test_verified_exhaustion",
            basis_ref="idea-exhausted-basis",
            basis_receipt=basis_receipt,
            idempotency_key="stage-disposition-idea-exhausted",
        )
        assert committed.request_ref == request.request_ref
        assert committed.run_ref == run.run_ref
        assert committed.run_completion_receipt == completion_receipt
        assert committed.disposition == "exhausted"
        assert engine.query_foreground(exhausted["quest_ref"])["stage"] == "reasoning"
    finally:
        exhausted_runtime.close()


def test_forced_switch_revokes_old_epoch_and_fence_before_new_grant(
    tmp_path: Path,
) -> None:
    runtime = _runtime(prepare_data_root(tmp_path / "forced-switch"), _IdeaProvider())
    try:
        completed = _confirm_question_with_prefix(runtime, "forced-switch")
        root_question, request, run = _admit_direct_idea_request(
            runtime, completed, "forced-switch"
        )
        human = runtime.owners.human_collaboration
        seeded = _confirm_waived_manual_question(
            human,
            quest_ref=completed["quest_ref"],
            parent_question_ref=root_question.question_ref,
            key_prefix="forced-switch-target",
        )
        for _boundary in range(4):
            if not human.reconcile_once():
                break
        target = runtime.owners.research_graph.query_question_tree(
            completed["quest_ref"]
        )[-1]
        assert target.context_ref == seeded["context_ref"]
        old_foreground = runtime.owners.advancement_engine.query_foreground(
            completed["quest_ref"]
        )
        assert old_foreground is not None

        confirmed = _confirmed_control(
            human,
            scope_ref=f"quest:{completed['quest_ref']}",
            payload={
                "action": "forced_switch",
                "target": {
                    "quest_ref": completed["quest_ref"],
                    "cycle_ref": completed["cycle_ref"],
                    "question_ref": old_foreground["question_ref"],
                    "epoch": old_foreground["epoch"],
                    "target_question_ref": target.question_ref,
                },
                "reason": "operator_requested",
            },
            key="forced-switch",
        )
        switched = _execute_control(human, confirmed, "forced-switch")
        assert switched["executed"] is True
        next_foreground = runtime.owners.advancement_engine.query_foreground(
            completed["quest_ref"]
        )
        assert next_foreground is not None
        assert next_foreground["question_ref"] == target.question_ref
        assert next_foreground["cycle_ref"] != completed["cycle_ref"]
        assert next_foreground["epoch"] == old_foreground["epoch"] + 1
        assert next_foreground["status"] == "active"

        old_run = runtime.owners.agent_runtime.query_managed_run(run.run_ref)
        assert old_run is not None
        assert old_run["status"] == "suspended_fenced"
        assert old_run["safe_point_ref"]
        replayed_admission = runtime.owners.agent_runtime.admit_idea_stage(
            request,
            "forced-switch-admit",
            runtime_binding=_runtime_binding("forced-switch"),
        )
        assert replayed_admission.run_ref == run.run_ref
        assert replayed_admission.attempt_ref == run.attempt_ref
        assert replayed_admission.status == "suspended_fenced"
        with pytest.raises(OwnerConflict, match="idempotency_conflict"):
            runtime.owners.agent_runtime.admit_idea_stage(
                request,
                "forced-switch-admit",
                runtime_binding=_runtime_binding("forced-switch-drift"),
            )
        with pytest.raises(OwnerConflict, match="runtime_fence_revoked"):
            runtime.owners.agent_runtime.record_idea_primary_draft(
                run_ref=run.run_ref,
                attempt_ref=run.attempt_ref,
                fence_ref=run.fence_ref,
                native_session_ref="native-late-old-epoch",
                runtime_binding=run.runtime_binding,
                draft={"kind": "NoViableCandidate"},
                adapter_kind="test_control",
                idempotency_key="forced-switch-late-old-epoch",
            )
        with pytest.raises(OwnerConflict, match="stage_request_epoch_revoked"):
            late_receipt = AcceptanceReceipt(
                issuer="agent_runtime",
                kind="run_completion",
                receipt_ref="late-receipt",
                subject_ref=run.run_ref,
                payload_hash="0" * 64,
            )
            runtime.owners.advancement_engine.commit_idea_stage(
                request_ref=request.request_ref,
                run_ref=run.run_ref,
                outcome_ref="late-outcome",
                outcome_kind="idea_set",
                run_completion_receipt=late_receipt,
                outcome_receipt=late_receipt,
                idempotency_key="forced-switch-late-commit",
            )
    finally:
        runtime.close()


def test_forced_switch_provider_cleanup_reconciles_after_daemon_restart(
    tmp_path: Path,
) -> None:
    class _CleanupIdeaProvider(_IdeaProvider):
        def __init__(self) -> None:
            super().__init__()
            self.reconciled_operations: list[str] = []

        def reconcile_cancelled_job(self, job_ref: str) -> bool:
            self.reconciled_operations.append(job_ref)
            return True

    data_root = prepare_data_root(tmp_path / "forced-switch-provider-cleanup")
    runtime = _runtime(data_root, _CleanupIdeaProvider())
    completed = _confirm_question_with_prefix(
        runtime, "forced-switch-provider-cleanup"
    )
    root_question, _request, run = _admit_direct_idea_request(
        runtime, completed, "forced-switch-provider-cleanup"
    )
    runtime.owners.agent_runtime.begin_provider_unit(
        unit_ref=run.primary_invocation.invocation_ref,
        operation_ref=run.primary_invocation.operation_ref,
        run_ref=run.run_ref,
        attempt_ref=run.attempt_ref,
        fence_ref=run.fence_ref,
        unit_kind="idea_primary",
    )
    human = runtime.owners.human_collaboration
    _confirm_waived_manual_question(
        human,
        quest_ref=completed["quest_ref"],
        parent_question_ref=root_question.question_ref,
        key_prefix="forced-switch-provider-cleanup-target",
    )
    for _boundary in range(4):
        if not human.reconcile_once():
            break
    target = runtime.owners.research_graph.query_question_tree(
        completed["quest_ref"]
    )[-1]
    foreground = runtime.owners.advancement_engine.query_foreground(
        completed["quest_ref"]
    )
    assert foreground is not None
    command = _confirmed_control(
        human,
        scope_ref=f"quest:{completed['quest_ref']}",
        payload={
            "action": "forced_switch",
            "target": {
                "quest_ref": completed["quest_ref"],
                "cycle_ref": completed["cycle_ref"],
                "question_ref": foreground["question_ref"],
                "epoch": foreground["epoch"],
                "target_question_ref": target.question_ref,
            },
            "reason": "operator_requested",
        },
        key="forced-switch-provider-cleanup",
    )
    _execute_control(human, command, "forced-switch-provider-cleanup")
    controlled = runtime.owners.agent_runtime.query_managed_run(run.run_ref)
    assert controlled is not None
    assert controlled["status"] == "suspended_fenced"
    assert controlled["cleanup_status"] == "pending"
    runtime.close()

    provider = _CleanupIdeaProvider()
    restarted = _runtime(data_root, provider)
    try:
        pending = restarted.owners.agent_runtime.query_managed_run(run.run_ref)
        assert pending is not None
        assert pending["cleanup_status"] == "pending"
        assert restarted.idea_stage.process_once() is True
        assert provider.reconciled_operations == [
            run.primary_invocation.operation_ref
        ]
        cleaned = restarted.owners.agent_runtime.query_managed_run(run.run_ref)
        assert cleaned is not None
        assert cleaned["status"] == "suspended_fenced"
        assert cleaned["cleanup_status"] == "completed"
        with restarted._database.read() as connection:
            unit_status = connection.execute(
                text(
                    "SELECT status FROM ar_provider_units WHERE unit_ref = :unit_ref"
                ),
                {"unit_ref": run.primary_invocation.invocation_ref},
            ).scalar_one()
        assert unit_status == "revoked"
    finally:
        restarted.close()


def test_revoked_epoch_request_cannot_admit_a_new_stage_run(tmp_path: Path) -> None:
    runtime = _runtime(
        prepare_data_root(tmp_path / "revoked-request-admission"), _IdeaProvider()
    )
    try:
        completed = _confirm_question_with_prefix(runtime, "revoked-request-admission")
        root_question, request = _prepare_direct_idea_request(
            runtime, completed, "revoked-request-admission"
        )
        seeded = _confirm_waived_manual_question(
            runtime.owners.human_collaboration,
            quest_ref=completed["quest_ref"],
            parent_question_ref=root_question.question_ref,
            key_prefix="revoked-request-target",
        )
        for _boundary in range(4):
            if not runtime.owners.human_collaboration.reconcile_once():
                break
        target = runtime.owners.research_graph.query_question_tree(
            completed["quest_ref"]
        )[-1]
        assert target.context_ref == seeded["context_ref"]
        foreground = runtime.owners.advancement_engine.query_foreground(
            completed["quest_ref"]
        )
        assert foreground is not None
        command = _confirmed_control(
            runtime.owners.human_collaboration,
            scope_ref=f"quest:{completed['quest_ref']}",
            payload={
                "action": "forced_switch",
                "target": {
                    "quest_ref": completed["quest_ref"],
                    "cycle_ref": completed["cycle_ref"],
                    "question_ref": foreground["question_ref"],
                    "epoch": foreground["epoch"],
                    "target_question_ref": target.question_ref,
                },
                "reason": "operator_requested",
            },
            key="revoked-request-switch",
        )
        _execute_control(
            runtime.owners.human_collaboration, command, "revoked-request-switch"
        )

        with pytest.raises(OwnerConflict, match="stage_run_request_not_current"):
            runtime.owners.agent_runtime.admit_idea_stage(
                request,
                "revoked-request-late-admit",
                runtime_binding=_runtime_binding("revoked-request-late-admit"),
            )
    finally:
        runtime.close()


def test_forced_switch_back_rebinds_logical_run_to_new_epoch_and_fence(
    tmp_path: Path,
) -> None:
    runtime = _runtime(prepare_data_root(tmp_path / "forced-switch-back"), _IdeaProvider())
    try:
        completed = _confirm_question_with_prefix(runtime, "forced-switch-back")
        runtime.idea_stage.start("forced-switch-back-start")
        root_question = runtime.owners.research_graph.query_question(
            completed["initialization_id"]
        )
        assert root_question is not None
        original_request = (
            runtime.owners.advancement_engine.query_idea_stage_request(
                completed["cycle_ref"]
            )
        )
        assert original_request is not None
        original_run = runtime.owners.agent_runtime.query_idea_stage_run(
            original_request.request_ref
        )
        assert original_run is not None

        human = runtime.owners.human_collaboration
        _confirm_waived_manual_question(
            human,
            quest_ref=completed["quest_ref"],
            parent_question_ref=root_question.question_ref,
            key_prefix="forced-switch-back-target",
        )
        for _boundary in range(4):
            if not human.reconcile_once():
                break
        child = runtime.owners.research_graph.query_question_tree(
            completed["quest_ref"]
        )[-1]
        source = runtime.owners.advancement_engine.query_foreground(
            completed["quest_ref"]
        )
        assert source is not None
        switch_to_child = _confirmed_control(
            human,
            scope_ref=f"quest:{completed['quest_ref']}",
            payload={
                "action": "forced_switch",
                "target": {
                    "quest_ref": completed["quest_ref"],
                    "cycle_ref": source["cycle_ref"],
                    "question_ref": source["question_ref"],
                    "epoch": source["epoch"],
                    "target_question_ref": child.question_ref,
                },
                "reason": "operator_requested",
            },
            key="forced-switch-back-child",
        )
        _execute_control(human, switch_to_child, "forced-switch-back-child")

        child_foreground = runtime.owners.advancement_engine.query_foreground(
            completed["quest_ref"]
        )
        assert child_foreground is not None
        switch_to_root = _confirmed_control(
            human,
            scope_ref=f"quest:{completed['quest_ref']}",
            payload={
                "action": "forced_switch",
                "target": {
                    "quest_ref": completed["quest_ref"],
                    "cycle_ref": child_foreground["cycle_ref"],
                    "question_ref": child_foreground["question_ref"],
                    "epoch": child_foreground["epoch"],
                    "target_question_ref": root_question.question_ref,
                },
                "reason": "operator_requested",
            },
            key="forced-switch-back-root",
        )
        _execute_control(human, switch_to_root, "forced-switch-back-root")

        restored = runtime.owners.advancement_engine.query_foreground(
            completed["quest_ref"]
        )
        assert restored is not None
        assert restored["cycle_ref"] == completed["cycle_ref"]
        assert restored["question_ref"] == root_question.question_ref
        assert restored["epoch"] == int(source["epoch"]) + 2

        runtime.idea_stage.start("forced-switch-back-resume")
        rebound_request = runtime.owners.advancement_engine.query_idea_stage_request(
            completed["cycle_ref"]
        )
        assert rebound_request is not None
        assert rebound_request.request_ref != original_request.request_ref
        assert rebound_request.epoch == restored["epoch"]
        rebound_run = runtime.owners.agent_runtime.query_idea_stage_run(
            rebound_request.request_ref
        )
        assert rebound_run is not None
        assert rebound_run.run_ref == original_run.run_ref
        assert rebound_run.root_session_ref == original_run.root_session_ref
        assert rebound_run.attempt_ref != original_run.attempt_ref
        assert rebound_run.fence_ref != original_run.fence_ref
        assert rebound_run.technical_predecessor_attempt_ref == original_run.attempt_ref
        managed = runtime.owners.agent_runtime.query_managed_run(original_run.run_ref)
        assert managed is not None
        assert managed["status"] == "running"
        assert managed["epoch"] == restored["epoch"]
        assert managed["attempt_ref"] == rebound_run.attempt_ref
        assert managed["fence_ref"] == rebound_run.fence_ref
        with pytest.raises(OwnerConflict, match="runtime_fence_revoked"):
            runtime.owners.agent_runtime.record_idea_primary_draft(
                run_ref=original_run.run_ref,
                attempt_ref=original_run.attempt_ref,
                fence_ref=original_run.fence_ref,
                native_session_ref="native-switch-back-old-epoch",
                runtime_binding=original_run.runtime_binding,
                draft={"kind": "NoViableCandidate"},
                adapter_kind="test_control",
                idempotency_key="forced-switch-back-late-old-epoch",
            )
    finally:
        runtime.close()


def test_normal_switch_keeps_source_grant_until_stage_commit_then_hands_off(
    tmp_path: Path,
) -> None:
    runtime = _runtime(prepare_data_root(tmp_path / "normal-switch"), _IdeaProvider())
    try:
        completed = _confirm_question_with_prefix(runtime, "normal-switch")
        runtime.idea_stage.start("normal-switch-stage-start")
        root_question = runtime.owners.research_graph.query_question(
            completed["initialization_id"]
        )
        assert root_question is not None
        request = runtime.owners.advancement_engine.query_idea_stage_request(
            completed["cycle_ref"]
        )
        assert request is not None
        run = runtime.owners.agent_runtime.query_idea_stage_run(request.request_ref)
        assert run is not None
        human = runtime.owners.human_collaboration
        _confirm_waived_manual_question(
            human,
            quest_ref=completed["quest_ref"],
            parent_question_ref=root_question.question_ref,
            key_prefix="normal-switch-target",
        )
        for _boundary in range(4):
            if not human.reconcile_once():
                break
        target = runtime.owners.research_graph.query_question_tree(
            completed["quest_ref"]
        )[-1]
        source = runtime.owners.advancement_engine.query_foreground(
            completed["quest_ref"]
        )
        assert source is not None
        confirmed = _confirmed_control(
            human,
            scope_ref=f"quest:{completed['quest_ref']}",
            payload={
                "action": "normal_switch",
                "target": {
                    "quest_ref": completed["quest_ref"],
                    "cycle_ref": completed["cycle_ref"],
                    "question_ref": source["question_ref"],
                    "epoch": source["epoch"],
                    "target_question_ref": target.question_ref,
                },
                "reason": "operator_requested",
            },
            key="normal-switch",
        )

        pending = _execute_control(human, confirmed, "normal-switch")
        assert pending["executed"] is False
        assert pending["control_pending"]["status"] == "handoff_pending"
        still_source = runtime.owners.advancement_engine.query_foreground(
            completed["quest_ref"]
        )
        assert still_source is not None
        assert still_source["cycle_ref"] == completed["cycle_ref"]
        assert still_source["grant_status"] == "active"
        assert runtime.owners.agent_runtime.query_managed_run(run.run_ref)[
            "status"
        ] == "running"

        for _boundary in range(8):
            runtime.idea_stage.process_once()
            current = runtime.owners.advancement_engine.query_foreground(
                completed["quest_ref"]
            )
            if current is not None and current["question_ref"] == target.question_ref:
                break
        handed_off = runtime.owners.advancement_engine.query_foreground(
            completed["quest_ref"]
        )
        assert handed_off is not None
        assert handed_off["question_ref"] == target.question_ref
        assert handed_off["epoch"] == source["epoch"] + 1
        assert handed_off["grant_status"] == "active"

        recovered = _execute_control(human, confirmed, "normal-switch-recover")
        assert recovered["executed"] is True
        assert recovered["control_execution"]["status"] == "completed"
    finally:
        runtime.close()


def test_normal_switch_revalidates_target_before_actual_handoff(
    tmp_path: Path,
) -> None:
    runtime = _runtime(
        prepare_data_root(tmp_path / "normal-switch-target-revalidation"),
        _IdeaProvider(),
    )
    try:
        completed = _confirm_question_with_prefix(
            runtime, "normal-switch-target-revalidation"
        )
        runtime.idea_stage.start("normal-switch-target-revalidation-stage-start")
        root = runtime.owners.research_graph.query_question(
            completed["initialization_id"]
        )
        assert root is not None
        request = runtime.owners.advancement_engine.query_idea_stage_request(
            completed["cycle_ref"]
        )
        assert request is not None
        run = runtime.owners.agent_runtime.query_idea_stage_run(request.request_ref)
        assert run is not None
        human = runtime.owners.human_collaboration
        _confirm_waived_manual_question(
            human,
            quest_ref=completed["quest_ref"],
            parent_question_ref=root.question_ref,
            key_prefix="normal-switch-target-revalidation-target",
        )
        for _boundary in range(4):
            if not human.reconcile_once():
                break
        target = runtime.owners.research_graph.query_question_tree(
            completed["quest_ref"]
        )[-1]
        source = runtime.owners.advancement_engine.query_foreground(
            completed["quest_ref"]
        )
        assert source is not None
        switch = _confirmed_control(
            human,
            scope_ref=f"quest:{completed['quest_ref']}",
            payload={
                "action": "normal_switch",
                "target": {
                    "quest_ref": completed["quest_ref"],
                    "cycle_ref": source["cycle_ref"],
                    "question_ref": source["question_ref"],
                    "epoch": source["epoch"],
                    "target_question_ref": target.question_ref,
                },
                "reason": "operator_requested",
            },
            key="normal-switch-target-revalidation",
        )
        pending = _execute_control(
            human, switch, "normal-switch-target-revalidation"
        )
        assert pending["control_pending"]["status"] == "handoff_pending"
        operation_ref = pending["control_pending"]["operation_ref"]

        prune_payload = {
            "action": "prune",
            "target": {
                "quest_ref": completed["quest_ref"],
                "cycle_ref": source["cycle_ref"],
                "question_ref": source["question_ref"],
                "epoch": source["epoch"],
                "target_question_ref": target.question_ref,
            },
            "reason": "target_invalidated_while_handoff_waited",
        }
        graph = runtime.owners.research_graph
        agent_runtime = runtime.owners.agent_runtime
        graph_revision = graph.query_snapshot().revision
        graph_reservation = graph.prepare_question_control(
            operation_ref="target-revalidation-prune-operation",
            payload=prune_payload,
            expected_revision=graph_revision,
            idempotency_key="target-revalidation-prune-rg-prepare",
        )
        affected_refs = tuple(graph_reservation["affected_question_refs"])
        runtime_revision = agent_runtime.query_snapshot().revision
        agent_runtime.prepare_runtime_control(
            operation_ref="target-revalidation-prune-operation",
            payload=prune_payload,
            expected_revision=runtime_revision,
            idempotency_key="target-revalidation-prune-ar-prepare",
            affected_question_refs=affected_refs,
        )
        runtime_receipt = agent_runtime.apply_runtime_control(
            operation_ref="target-revalidation-prune-operation",
            payload=prune_payload,
            expected_revision=runtime_revision,
            idempotency_key="target-revalidation-prune-ar-apply",
            affected_question_refs=affected_refs,
        )
        graph.apply_question_control(
            operation_ref="target-revalidation-prune-operation",
            payload=prune_payload,
            runtime_receipt=runtime_receipt,
            expected_revision=graph_revision,
            idempotency_key="target-revalidation-prune-rg-apply",
        )
        assert graph.query_question_lifecycle(target.question_ref)["status"] == "pruned"

        for _boundary in range(8):
            runtime.idea_stage.process_once()
            current = runtime.owners.advancement_engine.query_foreground(
                completed["quest_ref"]
            )
            if current is not None and current["stage"] == "plan":
                break
        retained = runtime.owners.advancement_engine.query_foreground(
            completed["quest_ref"]
        )
        assert retained is not None
        assert retained["cycle_ref"] == source["cycle_ref"]
        assert retained["question_ref"] == source["question_ref"]
        assert retained["stage"] == "plan"
        assert retained["grant_status"] == "active"
        assert retained["pending_operation_ref"] is None
        aborted = runtime.owners.advancement_engine.query_foreground_control_by_intent(
            switch["intent_id"]
        )
        assert aborted is not None
        assert aborted["operation_ref"] == operation_ref
        assert aborted["status"] == "aborted"
        assert runtime.owners.agent_runtime.query_managed_run(run.run_ref)[
            "status"
        ] == "completed"
        with pytest.raises(OwnerConflict, match="repreview_required"):
            _execute_control(
                human,
                switch,
                "normal-switch-target-revalidation-recover",
            )
        with runtime._database.read() as connection:
            assert connection.execute(
                text(
                    "SELECT status FROM hc_control_sagas WHERE intent_id = "
                    ":intent_id"
                ),
                {"intent_id": switch["intent_id"]},
            ).scalar_one() == "aborted"
            assert connection.execute(
                text(
                    "SELECT COUNT(*) FROM ar_control_compensations WHERE "
                    "operation_ref = :operation_ref"
                ),
                {"operation_ref": operation_ref},
            ).scalar_one() == 0
    finally:
        runtime.close()


def test_forced_switch_overrides_a_pending_normal_handoff(tmp_path: Path) -> None:
    runtime = _runtime(
        prepare_data_root(tmp_path / "forced-overrides-normal"), _IdeaProvider()
    )
    try:
        completed = _confirm_question_with_prefix(runtime, "forced-overrides-normal")
        runtime.idea_stage.start("forced-overrides-normal-stage-start")
        root = runtime.owners.research_graph.query_question(
            completed["initialization_id"]
        )
        assert root is not None
        human = runtime.owners.human_collaboration
        for index in (1, 2):
            _confirm_waived_manual_question(
                human,
                quest_ref=completed["quest_ref"],
                parent_question_ref=root.question_ref,
                key_prefix=f"forced-overrides-normal-target-{index}",
            )
            for _boundary in range(4):
                if not human.reconcile_once():
                    break
        targets = runtime.owners.research_graph.query_question_tree(
            completed["quest_ref"]
        )[-2:]
        source = runtime.owners.advancement_engine.query_foreground(
            completed["quest_ref"]
        )
        assert source is not None
        normal = _confirmed_control(
            human,
            scope_ref=f"quest:{completed['quest_ref']}",
            payload={
                "action": "normal_switch",
                "target": {
                    "quest_ref": completed["quest_ref"],
                    "cycle_ref": source["cycle_ref"],
                    "question_ref": source["question_ref"],
                    "epoch": source["epoch"],
                    "target_question_ref": targets[0].question_ref,
                },
                "reason": "operator_requested",
            },
            key="forced-overrides-normal-pending",
        )
        pending = _execute_control(human, normal, "forced-overrides-normal-pending")
        assert pending["control_pending"]["status"] == "handoff_pending"

        forced = _confirmed_control(
            human,
            scope_ref=f"quest:{completed['quest_ref']}",
            payload={
                "action": "forced_switch",
                "target": {
                    "quest_ref": completed["quest_ref"],
                    "cycle_ref": source["cycle_ref"],
                    "question_ref": source["question_ref"],
                    "epoch": source["epoch"],
                    "target_question_ref": targets[1].question_ref,
                },
                "reason": "operator_override",
            },
            key="forced-overrides-normal-forced",
        )
        executed = _execute_control(human, forced, "forced-overrides-normal-forced")
        assert executed["executed"] is True
        foreground = runtime.owners.advancement_engine.query_foreground(
            completed["quest_ref"]
        )
        assert foreground is not None
        assert foreground["question_ref"] == targets[1].question_ref
        old_operation = (
            runtime.owners.advancement_engine.query_foreground_control_by_intent(
                normal["intent_id"]
            )
        )
        assert old_operation is not None
        assert old_operation["status"] == "aborted"
    finally:
        runtime.close()


def test_forced_switch_compensates_runtime_when_target_fails_handoff_revalidation(
    tmp_path: Path,
) -> None:
    runtime = _runtime(
        prepare_data_root(tmp_path / "forced-switch-target-invalidated"),
        _IdeaProvider(),
    )
    try:
        completed = _confirm_question_with_prefix(
            runtime, "forced-switch-target-invalidated"
        )
        root, _request, run = _admit_direct_idea_request(
            runtime,
            completed,
            "forced-switch-target-invalidated",
        )
        human = runtime.owners.human_collaboration
        _confirm_waived_manual_question(
            human,
            quest_ref=completed["quest_ref"],
            parent_question_ref=root.question_ref,
            key_prefix="forced-switch-target-invalidated-target",
        )
        for _boundary in range(4):
            if not human.reconcile_once():
                break
        target = runtime.owners.research_graph.query_question_tree(
            completed["quest_ref"]
        )[-1]
        source = runtime.owners.advancement_engine.query_foreground(
            completed["quest_ref"]
        )
        assert source is not None
        command = _confirmed_control(
            human,
            scope_ref=f"quest:{completed['quest_ref']}",
            payload={
                "action": "forced_switch",
                "target": {
                    "quest_ref": completed["quest_ref"],
                    "cycle_ref": source["cycle_ref"],
                    "question_ref": source["question_ref"],
                    "epoch": source["epoch"],
                    "target_question_ref": target.question_ref,
                },
                "reason": "operator_requested",
            },
            key="forced-switch-target-invalidated",
        )

        class _InvalidatedTargetVerifier:
            def verify_current_question(self, **_values) -> None:
                raise OwnerConflict("research_control_question_not_present")

        runtime.owners.advancement_engine._current_question_verifier = (
            _InvalidatedTargetVerifier()
        )
        with pytest.raises(OwnerConflict, match="research_control_repreview_required"):
            _execute_control(
                human,
                command,
                "forced-switch-target-invalidated",
            )

        retained = runtime.owners.advancement_engine.query_foreground(
            completed["quest_ref"]
        )
        assert retained is not None
        assert retained["cycle_ref"] == source["cycle_ref"]
        assert retained["question_ref"] == source["question_ref"]
        assert retained["grant_status"] == "active"
        assert retained["pending_operation_ref"] is None
        recovered = runtime.owners.agent_runtime.query_managed_run(run.run_ref)
        assert recovered is not None
        assert recovered["status"] == "running"
        assert recovered["run_ref"] == run.run_ref
        assert recovered["root_session_ref"] == run.root_session_ref
        assert recovered["attempt_ref"] != run.attempt_ref
        assert recovered["fence_ref"] != run.fence_ref
        operation = (
            runtime.owners.advancement_engine.query_foreground_control_by_intent(
                command["intent_id"]
            )
        )
        assert operation is not None
        assert operation["status"] == "aborted"
        with runtime._database.read() as connection:
            assert connection.execute(
                text(
                    "SELECT status FROM hc_control_sagas WHERE intent_id = "
                    ":intent_id"
                ),
                {"intent_id": command["intent_id"]},
            ).scalar_one() == "aborted"
    finally:
        runtime.close()


def test_target_invalidated_switch_compensation_recovers_after_daemon_restart(
    tmp_path: Path,
) -> None:
    data_root = prepare_data_root(tmp_path / "target-invalidated-compensation-restart")
    runtime = _runtime(data_root, _IdeaProvider())
    completed = _confirm_question_with_prefix(
        runtime, "target-invalidated-compensation-restart"
    )
    root, _request, run = _admit_direct_idea_request(
        runtime,
        completed,
        "target-invalidated-compensation-restart",
    )
    human = runtime.owners.human_collaboration
    _confirm_waived_manual_question(
        human,
        quest_ref=completed["quest_ref"],
        parent_question_ref=root.question_ref,
        key_prefix="target-invalidated-compensation-restart-target",
    )
    for _boundary in range(4):
        if not human.reconcile_once():
            break
    target = runtime.owners.research_graph.query_question_tree(
        completed["quest_ref"]
    )[-1]
    source = runtime.owners.advancement_engine.query_foreground(
        completed["quest_ref"]
    )
    assert source is not None
    command = _confirmed_control(
        human,
        scope_ref=f"quest:{completed['quest_ref']}",
        payload={
            "action": "forced_switch",
            "target": {
                "quest_ref": completed["quest_ref"],
                "cycle_ref": source["cycle_ref"],
                "question_ref": source["question_ref"],
                "epoch": source["epoch"],
                "target_question_ref": target.question_ref,
            },
            "reason": "operator_requested",
        },
        key="target-invalidated-compensation-restart",
    )

    class _InvalidatedTargetVerifier:
        def verify_current_question(self, **_values) -> None:
            raise OwnerConflict("research_control_question_not_present")

    runtime.owners.advancement_engine._current_question_verifier = (
        _InvalidatedTargetVerifier()
    )

    def crash_before_compensation(**_values):
        raise OwnerConflict("simulated_compensation_crash")

    runtime.owners.agent_runtime.compensate_runtime_control = (
        crash_before_compensation
    )
    with pytest.raises(OwnerConflict, match="simulated_compensation_crash"):
        _execute_control(
            human,
            command,
            "target-invalidated-compensation-restart",
        )
    operation = runtime.owners.advancement_engine.query_foreground_control_by_intent(
        command["intent_id"]
    )
    assert operation is not None
    assert operation["status"] == "aborted"
    assert operation["abort_reason_code"] == "switch_target_invalidated"
    interrupted = runtime.owners.agent_runtime.query_managed_run(run.run_ref)
    assert interrupted is not None
    assert interrupted["status"] == "suspended_fenced"
    with runtime._database.read() as connection:
        assert connection.execute(
            text(
                "SELECT status FROM hc_control_sagas WHERE intent_id = :intent_id"
            ),
            {"intent_id": command["intent_id"]},
        ).scalar_one() == "runtime_applied"
    runtime.close()

    restarted = _runtime(data_root, _IdeaProvider())
    try:
        compensated = restarted.owners.agent_runtime.query_runtime_control_compensation(
            operation["operation_ref"]
        )
        assert compensated is not None
        recovered = restarted.owners.agent_runtime.query_managed_run(run.run_ref)
        assert recovered is not None
        assert recovered["status"] == "running"
        assert recovered["run_ref"] == run.run_ref
        assert recovered["root_session_ref"] == run.root_session_ref
        assert recovered["attempt_ref"] != run.attempt_ref
        assert recovered["fence_ref"] != run.fence_ref
        with restarted._database.read() as connection:
            assert connection.execute(
                text(
                    "SELECT status FROM hc_control_sagas WHERE intent_id = "
                    ":intent_id"
                ),
                {"intent_id": command["intent_id"]},
            ).scalar_one() == "aborted"
    finally:
        restarted.close()


def test_prune_and_restore_are_rg_lifecycle_facts_not_question_deletion(
    tmp_path: Path,
) -> None:
    runtime = _runtime(prepare_data_root(tmp_path / "prune-restore"), _IdeaProvider())
    try:
        completed = _confirm_question_with_prefix(runtime, "prune-restore")
        root = runtime.owners.research_graph.query_question(
            completed["initialization_id"]
        )
        assert root is not None
        seeded = _confirm_waived_manual_question(
            runtime.owners.human_collaboration,
            quest_ref=completed["quest_ref"],
            parent_question_ref=root.question_ref,
            key_prefix="prune-restore-child",
        )
        for _boundary in range(4):
            if not runtime.owners.human_collaboration.reconcile_once():
                break
        child = runtime.owners.research_graph.query_question_tree(
            completed["quest_ref"]
        )[-1]
        assert child.context_ref == seeded["context_ref"]
        foreground = runtime.owners.advancement_engine.query_foreground(
            completed["quest_ref"]
        )
        assert foreground is not None
        base_target = {
            "quest_ref": completed["quest_ref"],
            "cycle_ref": completed["cycle_ref"],
            "question_ref": foreground["question_ref"],
            "epoch": foreground["epoch"],
            "target_question_ref": child.question_ref,
        }
        pruned_command = _confirmed_control(
            runtime.owners.human_collaboration,
            scope_ref=f"quest:{completed['quest_ref']}",
            payload={
                "action": "prune",
                "target": base_target,
                "reason": "operator_requested",
            },
            key="prune-restore-prune",
        )
        assert [
            item["source_owner"]
            for item in pruned_command["impact_preview"]["owner_previews"]
        ] == ["advancement_engine", "agent_runtime", "research_graph"]
        executed_prune = _execute_control(
            runtime.owners.human_collaboration,
            pruned_command,
            "prune-restore-prune",
        )
        owner_receipts = executed_prune["control_execution"]["owner_receipts"]
        prune_record_ref = owner_receipts[-1]["prune_record_ref"]
        _drop_control_execution_receipt(runtime, pruned_command["intent_id"])
        reconciled_prune = _execute_control(
            runtime.owners.human_collaboration,
            pruned_command,
            "prune-restore-prune-new-browser",
        )
        assert reconciled_prune["control_execution"]["owner_receipts"] == owner_receipts
        assert runtime.owners.research_graph.query_question_history_by_ref(
            child.question_ref
        ) is not None
        assert runtime.owners.research_graph.query_question_by_ref(
            child.question_ref
        ) is None
        assert runtime.owners.research_graph.query_question_lifecycle(
            child.question_ref
        )["status"] == "pruned"
        with pytest.raises(OwnerConflict, match="research_control_question_not_present"):
            _confirmed_control(
                runtime.owners.human_collaboration,
                scope_ref=f"quest:{completed['quest_ref']}",
                payload={
                    "action": "forced_switch",
                    "target": {
                        **base_target,
                        "target_scope": "cycle",
                    },
                    "reason": "operator_requested",
                },
                key="prune-restore-switch-pruned",
            )
        assert child.question_ref not in {
            item["question_ref"]
            for item in runtime.projection.query_snapshot()["question_tree"]["items"]
        }
        recovery_records = runtime.projection.query_snapshot()["research_control"][
            "recovery_records"
        ]
        assert recovery_records == [
            {
                "prune_record_ref": prune_record_ref,
                "quest_ref": completed["quest_ref"],
                "root_question_ref": child.question_ref,
                "affected_question_refs": [child.question_ref],
                "affected_question_count": 1,
                "receipt_ref": owner_receipts[-1]["receipt_ref"],
                "receipt_hash": owner_receipts[-1]["receipt_hash"],
                "created_at": recovery_records[0]["created_at"],
            }
        ]
        assert runtime.owners.advancement_engine.query_foreground(
            completed["quest_ref"]
        )["status"] == "active"

        latest = runtime.owners.advancement_engine.query_foreground(
            completed["quest_ref"]
        )
        restored_command = _confirmed_control(
            runtime.owners.human_collaboration,
            scope_ref=f"quest:{completed['quest_ref']}",
            payload={
                "action": "restore",
                "target": {
                    **base_target,
                    "cycle_ref": latest["cycle_ref"],
                    "question_ref": latest["question_ref"],
                    "epoch": latest["epoch"],
                    "prune_record_ref": prune_record_ref,
                },
                "reason": "operator_requested",
            },
            key="prune-restore-restore",
        )
        _execute_control(
            runtime.owners.human_collaboration,
            restored_command,
            "prune-restore-restore",
        )
        assert runtime.owners.research_graph.query_question_lifecycle(
            child.question_ref
        )["status"] == "active"
        assert runtime.projection.query_snapshot()["research_control"][
            "recovery_records"
        ] == []
    finally:
        runtime.close()


def test_pruning_ancestor_quiesces_descendant_foreground_cycle(
    tmp_path: Path,
) -> None:
    runtime = _runtime(prepare_data_root(tmp_path / "ancestor-prune"), _IdeaProvider())
    try:
        completed = _confirm_question_with_prefix(runtime, "ancestor-prune")
        root = runtime.owners.research_graph.query_question(
            completed["initialization_id"]
        )
        assert root is not None
        _confirm_waived_manual_question(
            runtime.owners.human_collaboration,
            quest_ref=completed["quest_ref"],
            parent_question_ref=root.question_ref,
            key_prefix="ancestor-prune-child",
        )
        for _boundary in range(4):
            if not runtime.owners.human_collaboration.reconcile_once():
                break
        child = runtime.owners.research_graph.query_question_tree(
            completed["quest_ref"]
        )[-1]
        source = runtime.owners.advancement_engine.query_foreground(
            completed["quest_ref"]
        )
        assert source is not None
        switch = _confirmed_control(
            runtime.owners.human_collaboration,
            scope_ref=f"quest:{completed['quest_ref']}",
            payload={
                "action": "forced_switch",
                "target": {
                    "quest_ref": completed["quest_ref"],
                    "cycle_ref": source["cycle_ref"],
                    "question_ref": source["question_ref"],
                    "epoch": source["epoch"],
                    "target_question_ref": child.question_ref,
                },
                "reason": "operator_requested",
            },
            key="ancestor-prune-switch",
        )
        _execute_control(
            runtime.owners.human_collaboration,
            switch,
            "ancestor-prune-switch",
        )
        runtime.idea_stage.start("ancestor-prune-child-run")
        foreground = runtime.owners.advancement_engine.query_foreground(
            completed["quest_ref"]
        )
        assert foreground is not None
        child_request = runtime.owners.advancement_engine.query_idea_stage_request(
            foreground["cycle_ref"]
        )
        assert child_request is not None
        child_run = runtime.owners.agent_runtime.query_idea_stage_run(
            child_request.request_ref
        )
        assert child_run is not None

        prune = _confirmed_control(
            runtime.owners.human_collaboration,
            scope_ref=f"quest:{completed['quest_ref']}",
            payload={
                "action": "prune",
                "target": {
                    "quest_ref": completed["quest_ref"],
                    "cycle_ref": foreground["cycle_ref"],
                    "question_ref": child.question_ref,
                    "epoch": foreground["epoch"],
                    "target_question_ref": root.question_ref,
                },
                "reason": "operator_requested",
            },
            key="ancestor-prune-root",
        )
        executed = _execute_control(
            runtime.owners.human_collaboration,
            prune,
            "ancestor-prune-root",
        )
        runtime_receipt = executed["control_execution"]["owner_receipts"][1]
        assert runtime_receipt["affected_question_refs"] == [
            root.question_ref,
            child.question_ref,
        ]
        assert runtime_receipt["quiescence_receipt"][
            "affected_question_refs"
        ] == [root.question_ref, child.question_ref]
        suspended = runtime.owners.advancement_engine.query_foreground(
            completed["quest_ref"]
        )
        assert suspended is not None
        assert suspended["question_ref"] == child.question_ref
        assert suspended["status"] == "suspended"
        assert runtime.owners.agent_runtime.query_managed_run(child_run.run_ref)[
            "status"
        ] == "suspended_fenced"
        projected_child_run = runtime.owners.agent_runtime.query_idea_stage_run(
            child_request.request_ref
        )
        assert projected_child_run is not None
        assert projected_child_run.execution is None
        assert _public_run(projected_child_run)["fence_status"] == "revoked"
        assert runtime.owners.research_graph.query_question_by_ref(
            root.question_ref
        ) is None
        assert runtime.owners.research_graph.query_question_by_ref(
            child.question_ref
        ) is None
    finally:
        runtime.close()


def test_restore_of_foreground_question_requires_explicit_cycle_resume(
    tmp_path: Path,
) -> None:
    runtime = _runtime(prepare_data_root(tmp_path / "foreground-restore"), _IdeaProvider())
    try:
        completed = _confirm_question_with_prefix(runtime, "foreground-restore")
        runtime.idea_stage.start("foreground-restore-start")
        root = runtime.owners.research_graph.query_question(
            completed["initialization_id"]
        )
        assert root is not None
        foreground = runtime.owners.advancement_engine.query_foreground(
            completed["quest_ref"]
        )
        assert foreground is not None
        request = runtime.owners.advancement_engine.query_idea_stage_request(
            completed["cycle_ref"]
        )
        assert request is not None
        run = runtime.owners.agent_runtime.query_idea_stage_run(request.request_ref)
        assert run is not None
        target = {
            "quest_ref": completed["quest_ref"],
            "cycle_ref": completed["cycle_ref"],
            "question_ref": root.question_ref,
            "epoch": foreground["epoch"],
            "target_question_ref": root.question_ref,
        }
        prune = _confirmed_control(
            runtime.owners.human_collaboration,
            scope_ref=f"quest:{completed['quest_ref']}",
            payload={
                "action": "prune",
                "target": target,
                "reason": "operator_requested",
            },
            key="foreground-restore-prune",
        )
        pruned = _execute_control(
            runtime.owners.human_collaboration,
            prune,
            "foreground-restore-prune",
        )
        prune_record_ref = pruned["control_execution"]["owner_receipts"][-1][
            "prune_record_ref"
        ]
        suspended = runtime.owners.advancement_engine.query_foreground(
            completed["quest_ref"]
        )
        assert suspended is not None
        assert suspended["status"] == "suspended"
        assert suspended["grant_status"] == "suspended"
        fenced = runtime.owners.agent_runtime.query_managed_run(run.run_ref)
        assert fenced is not None
        assert fenced["status"] == "suspended_fenced"

        with pytest.raises(OwnerConflict, match="research_control_question_not_present"):
            _confirmed_control(
                runtime.owners.human_collaboration,
                scope_ref=f"quest:{completed['quest_ref']}",
                payload={
                    "action": "resume",
                    "target": {
                        "quest_ref": completed["quest_ref"],
                        "cycle_ref": completed["cycle_ref"],
                        "question_ref": root.question_ref,
                        "epoch": suspended["epoch"],
                    },
                    "reason": "operator_requested",
                },
                key="foreground-restore-premature-resume",
            )
        assert runtime.owners.advancement_engine.query_foreground(
            completed["quest_ref"]
        )["status"] == "suspended"
        assert runtime.owners.agent_runtime.query_managed_run(run.run_ref)[
            "status"
        ] == "suspended_fenced"

        restore = _confirmed_control(
            runtime.owners.human_collaboration,
            scope_ref=f"quest:{completed['quest_ref']}",
            payload={
                "action": "restore",
                "target": {**target, "prune_record_ref": prune_record_ref},
                "reason": "operator_requested",
            },
            key="foreground-restore-restore",
        )
        _execute_control(
            runtime.owners.human_collaboration,
            restore,
            "foreground-restore-restore",
        )
        assert runtime.owners.research_graph.query_question_by_ref(
            root.question_ref
        ) is not None
        restored = runtime.owners.advancement_engine.query_foreground(
            completed["quest_ref"]
        )
        assert restored is not None
        assert restored["status"] == "suspended"
        assert restored["grant_status"] == "suspended"
        recovered = runtime.owners.agent_runtime.query_managed_run(run.run_ref)
        assert recovered is not None
        assert recovered["status"] == "suspended"
        assert recovered["root_session_ref"] == run.root_session_ref
        assert recovered["attempt_ref"] != run.attempt_ref
        assert recovered["fence_ref"] != run.fence_ref

        resume = _confirmed_control(
            runtime.owners.human_collaboration,
            scope_ref=f"quest:{completed['quest_ref']}",
            payload={
                "action": "resume",
                "target": {
                    "quest_ref": completed["quest_ref"],
                    "cycle_ref": completed["cycle_ref"],
                    "question_ref": root.question_ref,
                    "epoch": restored["epoch"],
                },
                "reason": "operator_requested",
            },
            key="foreground-restore-resume",
        )
        _execute_control(
            runtime.owners.human_collaboration,
            resume,
            "foreground-restore-resume",
        )
        active = runtime.owners.advancement_engine.query_foreground(
            completed["quest_ref"]
        )
        assert active is not None
        assert active["status"] == "active"
        assert active["grant_status"] == "active"
        assert runtime.owners.agent_runtime.query_managed_run(run.run_ref)[
            "status"
        ] == "running"
    finally:
        runtime.close()


def test_authenticated_web_control_executes_only_after_confirmation_and_projects_state(
    tmp_path: Path,
) -> None:
    runtime = _runtime(prepare_data_root(tmp_path / "web-control"), _IdeaProvider())
    try:
        completed = _confirm_question_with_prefix(runtime, "web-control")
        _question, _request, run = _admit_direct_idea_request(
            runtime, completed, "web-control"
        )
        foreground = runtime.owners.advancement_engine.query_foreground(
            completed["quest_ref"]
        )
        assert foreground is not None
        client, auth = _authenticated_client(runtime)
        payload = {
            "action": "pause",
            "target": {
                "quest_ref": completed["quest_ref"],
                "cycle_ref": completed["cycle_ref"],
                "question_ref": foreground["question_ref"],
                "epoch": foreground["epoch"],
            },
            "reason": "operator_requested",
        }

        drafted_response = client.post(
            "/api/v1/human-collaboration/commands",
            headers=_write_headers(auth, "web-control-draft"),
            json={
                "scope_ref": f"quest:{completed['quest_ref']}",
                "command": {
                    "command_kind": "research_control",
                    "payload": payload,
                },
            },
        )
        assert drafted_response.status_code == 201
        drafted = drafted_response.json()
        previewed_response = client.post(
            f"/api/v1/human-collaboration/commands/{drafted['intent_id']}/previews",
            headers=_write_headers(auth, "web-control-preview"),
            json={
                "draft_revision": drafted["draft_revision"],
                "draft_hash": drafted["draft_hash"],
            },
        )
        assert previewed_response.status_code == 201
        previewed = previewed_response.json()
        preview = previewed["impact_preview"]
        confirmed_response = client.post(
            f"/api/v1/human-collaboration/commands/{drafted['intent_id']}/confirmations",
            headers=_write_headers(auth, "web-control-confirm"),
            json={
                "draft_revision": drafted["draft_revision"],
                "draft_hash": drafted["draft_hash"],
                "preview_ref": preview["preview_ref"],
                "preview_hash": preview["preview_hash"],
            },
        )
        assert confirmed_response.status_code == 201
        confirmed = confirmed_response.json()
        assert runtime.owners.agent_runtime.query_managed_run(run.run_ref)[
            "status"
        ] == "running"

        executed_response = client.post(
            f"/api/v1/human-collaboration/commands/{drafted['intent_id']}/executions",
            headers=_write_headers(auth, "web-control-execute"),
            json={
                "confirmation_receipt_ref": confirmed["confirmation_receipt"][
                    "receipt_ref"
                ]
            },
        )
        assert executed_response.status_code == 201
        executed = executed_response.json()
        assert executed["executed"] is True

        snapshot_response = client.get("/api/v1/snapshot")
        assert snapshot_response.status_code == 200
        control = snapshot_response.json()["research_control"]
        assert control["status"] == "ready"
        assert control["foreground"]["status"] == "suspended"
        projected_run = next(
            item for item in control["managed_runs"] if item["run_ref"] == run.run_ref
        )
        assert projected_run["status"] == "suspended"
        assert projected_run["safe_point_ref"]
    finally:
        runtime.close()


def test_quest_experiment_is_a_managed_run_and_pause_blocks_late_observations(
    tmp_path: Path,
) -> None:
    runtime = _experiment_runtime(tmp_path / "experiment-control")
    try:
        quest = _confirm_experiment_quest(runtime)
        admitted = runtime.experiment.start(
            ExperimentIntent(
                execution_request_ref="experiment-control-request",
                quest_ref=quest["quest_ref"],
                title="managed experiment",
                hypothesis="暂停后旧 Attempt 不得继续发布观察。",
                variant_parameter=0.25,
                sample_count=16,
            ),
            "experiment-control-start",
        )
        run_ref = admitted["execution"]["run_ref"]
        managed = runtime.owners.agent_runtime.query_managed_run(run_ref)
        assert managed is not None
        assert managed["run_kind"] == "experiment"
        running = runtime.owners.agent_runtime.claim_next_experiment()
        assert running is not None
        foreground = runtime.owners.advancement_engine.query_foreground(
            quest["quest_ref"]
        )
        assert foreground is not None
        command = _confirmed_control(
            runtime.owners.human_collaboration,
            scope_ref=f"quest:{quest['quest_ref']}",
            payload={
                "action": "pause",
                "target": {
                    "target_scope": "run",
                    "quest_ref": quest["quest_ref"],
                    "cycle_ref": foreground["cycle_ref"],
                    "question_ref": foreground["question_ref"],
                    "epoch": foreground["epoch"],
                    "run_ref": run_ref,
                },
                "reason": "operator_requested",
            },
            key="experiment-control-pause",
        )
        assert [
            item["source_owner"]
            for item in command["impact_preview"]["owner_previews"]
        ] == ["agent_runtime"]
        pause_finished = threading.Event()
        pause_errors: list[BaseException] = []

        def pause_experiment() -> None:
            try:
                _execute_control(
                    runtime.owners.human_collaboration,
                    command,
                    "experiment-control-pause",
                )
            except BaseException as error:  # pragma: no cover - asserted below
                pause_errors.append(error)
            finally:
                pause_finished.set()

        pause_worker = threading.Thread(target=pause_experiment)
        pause_worker.start()
        assert not pause_finished.wait(timeout=0.1)
        runtime.owners.agent_runtime.acknowledge_provider_safe_point(
            run_ref=running.run_ref,
            attempt_ref=running.attempt_ref,
            fence_ref=running.fence_ref,
        )
        pause_worker.join(timeout=5)
        assert not pause_worker.is_alive()
        assert pause_errors == []
        paused = runtime.owners.agent_runtime.query_managed_run(run_ref)
        assert paused is not None
        assert paused["status"] == "suspended"
        assert paused["safe_point_ref"]
        assert paused["attempt_ref"] != running.attempt_ref
        assert paused["root_session_ref"] == running.root_session_ref
        assert paused["fence_ref"] != running.fence_ref
        projected = runtime.experiment.query(
            admitted["identities"]["evaluation_attempt_ref"]
        )["execution"]
        assert projected["managed_status"] == "suspended"
        assert projected["fence_status"] == "current"
        with pytest.raises(OwnerConflict, match="runtime_run_suspended"):
            runtime.owners.agent_runtime.record_experiment_observation(
                run_ref=running.run_ref,
                attempt_ref=running.attempt_ref,
                fence_ref=running.fence_ref,
                kind="stdout",
                payload={"line": "late output", "stream": "stdout"},
                observed_at=1_720_000_009.0,
            )

        resume = _confirmed_control(
            runtime.owners.human_collaboration,
            scope_ref=f"quest:{quest['quest_ref']}",
            payload={
                "action": "resume",
                "target": {
                    "target_scope": "run",
                    "quest_ref": quest["quest_ref"],
                    "cycle_ref": foreground["cycle_ref"],
                    "question_ref": foreground["question_ref"],
                    "epoch": foreground["epoch"],
                    "run_ref": run_ref,
                },
                "reason": "operator_requested",
            },
            key="experiment-control-resume",
        )
        _execute_control(
            runtime.owners.human_collaboration,
            resume,
            "experiment-control-resume",
        )
        resumed = runtime.owners.agent_runtime.claim_next_experiment()
        assert resumed is not None
        assert resumed.run_ref == running.run_ref
        assert resumed.root_session_ref == running.root_session_ref
        assert resumed.attempt_ref == paused["attempt_ref"]
        assert resumed.fence_ref == paused["fence_ref"]
    finally:
        runtime.close()


def test_experiment_worker_accepts_terminal_stop_receipt_after_pause_already_applied(
    tmp_path: Path,
) -> None:
    started = threading.Event()
    stop_requested = threading.Event()
    release_terminal_receipt = threading.Event()

    class _AckBeforeWorkerReceiptProvider(_DeterministicExperimentProvider):
        def execute(self, request, observe):
            started.set()
            assert stop_requested.wait(timeout=5)
            assert release_terminal_receipt.wait(timeout=5)
            raise ExperimentProviderUnavailable(
                "experiment_provider_stopped", durable_outcome="terminal"
            )

        def cancel_job(self, job_ref: str) -> None:
            stop_requested.set()

        def reconcile_cancelled_job(self, job_ref: str) -> bool:
            # The provider-owned watcher has observed physical exit; the worker is
            # deliberately delayed before receiving that same terminal receipt.
            return stop_requested.is_set()

    runtime = _experiment_runtime(
        tmp_path / "experiment-pause-late-worker-receipt",
        _AckBeforeWorkerReceiptProvider(),
    )
    try:
        quest = _confirm_experiment_quest(runtime)
        admitted = runtime.experiment.start(
            ExperimentIntent(
                execution_request_ref="experiment-pause-late-worker-request",
                quest_ref=quest["quest_ref"],
                title="late provider stop receipt",
                hypothesis="控制提交与 worker 回执乱序不得制造失败。",
                variant_parameter=0.25,
                sample_count=16,
            ),
            "experiment-pause-late-worker-start",
        )
        worker_errors: list[BaseException] = []

        def execute_provider() -> None:
            try:
                runtime.experiment.process_once()
            except BaseException as error:  # pragma: no cover - asserted below
                worker_errors.append(error)

        worker = threading.Thread(target=execute_provider)
        worker.start()
        assert started.wait(timeout=5)
        foreground = runtime.owners.advancement_engine.query_foreground(
            quest["quest_ref"]
        )
        assert foreground is not None
        command = _confirmed_control(
            runtime.owners.human_collaboration,
            scope_ref=f"quest:{quest['quest_ref']}",
            payload={
                "action": "pause",
                "target": {
                    "target_scope": "run",
                    "quest_ref": quest["quest_ref"],
                    "cycle_ref": foreground["cycle_ref"],
                    "question_ref": foreground["question_ref"],
                    "epoch": foreground["epoch"],
                    "run_ref": admitted["execution"]["run_ref"],
                },
                "reason": "operator_requested",
            },
            key="experiment-pause-late-worker",
        )
        _execute_control(
            runtime.owners.human_collaboration,
            command,
            "experiment-pause-late-worker",
        )
        managed = runtime.owners.agent_runtime.query_managed_run(
            admitted["execution"]["run_ref"]
        )
        assert managed is not None
        assert managed["status"] == "suspended"

        release_terminal_receipt.set()
        worker.join(timeout=5)
        assert not worker.is_alive()
        assert worker_errors == []
        managed = runtime.owners.agent_runtime.query_managed_run(
            admitted["execution"]["run_ref"]
        )
        assert managed is not None
        assert managed["status"] == "suspended"
    finally:
        stop_requested.set()
        release_terminal_receipt.set()
        runtime.close()


def test_run_scoped_control_does_not_capture_an_independent_quest_run(
    tmp_path: Path,
) -> None:
    runtime = _experiment_runtime(tmp_path / "independent-run-control")
    try:
        quest = _confirm_experiment_quest(runtime)
        first = runtime.experiment.start(
            ExperimentIntent(
                execution_request_ref="independent-run-first",
                quest_ref=quest["quest_ref"],
                title="first managed experiment",
                hypothesis="Only this exact Run is paused.",
                variant_parameter=0.25,
                sample_count=16,
            ),
            "independent-run-first-start",
        )
        second = runtime.experiment.start(
            ExperimentIntent(
                execution_request_ref="independent-run-second",
                quest_ref=quest["quest_ref"],
                title="second managed experiment",
                hypothesis="A sibling Run remains independently runnable.",
                variant_parameter=0.5,
                sample_count=16,
            ),
            "independent-run-second-start",
        )
        foreground = runtime.owners.advancement_engine.query_foreground(
            quest["quest_ref"]
        )
        assert foreground is not None
        first_run_ref = first["execution"]["run_ref"]
        second_run_ref = second["execution"]["run_ref"]
        command = _confirmed_control(
            runtime.owners.human_collaboration,
            scope_ref=f"quest:{quest['quest_ref']}",
            payload={
                "action": "pause",
                "target": {
                    "target_scope": "run",
                    "quest_ref": quest["quest_ref"],
                    "cycle_ref": foreground["cycle_ref"],
                    "question_ref": foreground["question_ref"],
                    "epoch": foreground["epoch"],
                    "run_ref": first_run_ref,
                },
                "reason": "operator_requested",
            },
            key="independent-run-pause",
        )
        executed = _execute_control(
            runtime.owners.human_collaboration,
            command,
            "independent-run-pause",
        )
        receipt = executed["control_execution"]["owner_receipts"][0]
        assert [item["run_ref"] for item in receipt["affected_runs"]] == [
            first_run_ref
        ]
        assert runtime.owners.agent_runtime.query_managed_run(first_run_ref)[
            "status"
        ] == "suspended"
        assert runtime.owners.agent_runtime.query_managed_run(first_run_ref)[
            "cycle_ref"
        ] is None
        assert runtime.owners.agent_runtime.query_managed_run(first_run_ref)[
            "epoch"
        ] is None
        assert runtime.owners.agent_runtime.query_managed_run(second_run_ref)[
            "status"
        ] == "running"
    finally:
        runtime.close()


def test_run_scoped_control_recovers_lost_hc_receipt_after_restart(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "run-scope-receipt-recovery"
    runtime = _experiment_runtime(data_root)
    quest = _confirm_experiment_quest(runtime)
    admitted = runtime.experiment.start(
        ExperimentIntent(
            execution_request_ref="run-scope-receipt-recovery-request",
            quest_ref=quest["quest_ref"],
            title="recover AR-only control",
            hypothesis="HC saga 必须覆盖不经过 AE 的精确 Run 控制。",
            variant_parameter=0.3,
            sample_count=16,
        ),
        "run-scope-receipt-recovery-start",
    )
    run_ref = admitted["execution"]["run_ref"]
    foreground = runtime.owners.advancement_engine.query_foreground(quest["quest_ref"])
    assert foreground is not None
    command = _confirmed_control(
        runtime.owners.human_collaboration,
        scope_ref=f"quest:{quest['quest_ref']}",
        payload={
            "action": "pause",
            "target": {
                "target_scope": "run",
                "quest_ref": quest["quest_ref"],
                "cycle_ref": foreground["cycle_ref"],
                "question_ref": foreground["question_ref"],
                "epoch": foreground["epoch"],
                "run_ref": run_ref,
            },
            "reason": "operator_requested",
        },
        key="run-scope-receipt-recovery",
    )
    executed = _execute_control(
        runtime.owners.human_collaboration,
        command,
        "run-scope-receipt-recovery",
    )
    owner_receipts = executed["control_execution"]["owner_receipts"]
    _drop_control_execution_receipt(runtime, command["intent_id"])
    runtime.close()

    restarted = _experiment_runtime(data_root)
    try:
        recovered = restarted.owners.human_collaboration.query_command(
            command["intent_id"]
        )
        assert recovered["executed"] is True
        assert recovered["control_execution"]["owner_receipts"] == owner_receipts
        with restarted._database.read() as connection:
            assert connection.execute(
                text(
                    "SELECT status FROM hc_control_sagas WHERE intent_id = :intent_id"
                ),
                {"intent_id": command["intent_id"]},
            ).scalar_one() == "completed"
    finally:
        restarted.close()


def test_cancel_experiment_closes_canonical_run_and_releases_idle_admission(
    tmp_path: Path,
) -> None:
    started = threading.Event()
    emit_late_observation = threading.Event()
    late_observation_returned = threading.Event()
    release = threading.Event()

    class _BlockingExperimentProvider(_DeterministicExperimentProvider):
        def execute(self, request, observe):
            started.set()
            assert emit_late_observation.wait(timeout=5)
            observe(
                ExperimentObservation(
                    "telemetry",
                    {"phase": "still-running-after-cancel"},
                    1_720_000_100.0,
                )
            )
            late_observation_returned.set()
            assert release.wait(timeout=5)
            return super().execute(request, observe)

    runtime = _experiment_runtime(
        tmp_path / "cancel-experiment-control",
        _BlockingExperimentProvider(),
    )
    try:
        quest = _confirm_experiment_quest(runtime)
        admitted = runtime.experiment.start(
            ExperimentIntent(
                execution_request_ref="cancel-experiment-first",
                quest_ref=quest["quest_ref"],
                title="cancelled managed experiment",
                hypothesis="终止必须同步 canonical Run 与计数器。",
                variant_parameter=0.25,
                sample_count=16,
            ),
            "cancel-experiment-first-start",
            require_idle=True,
        )
        worker_errors: list[BaseException] = []

        def execute_provider() -> None:
            try:
                runtime.experiment.process_once()
            except BaseException as error:  # pragma: no cover - asserted below
                worker_errors.append(error)

        worker = threading.Thread(target=execute_provider)
        worker.start()
        assert started.wait(timeout=5)
        running = runtime.owners.agent_runtime.query_experiment_run(
            admitted["identities"]["evaluation_attempt_ref"]
        )
        assert running is not None
        foreground = runtime.owners.advancement_engine.query_foreground(
            quest["quest_ref"]
        )
        assert foreground is not None
        command = _confirmed_control(
            runtime.owners.human_collaboration,
            scope_ref=f"quest:{quest['quest_ref']}",
            payload={
                "action": "cancel",
                "target": {
                    "target_scope": "run",
                    "quest_ref": quest["quest_ref"],
                    "cycle_ref": foreground["cycle_ref"],
                    "question_ref": foreground["question_ref"],
                    "epoch": foreground["epoch"],
                    "run_ref": running.run_ref,
                },
                "reason": "operator_requested",
            },
            key="cancel-experiment",
        )
        _execute_control(
            runtime.owners.human_collaboration,
            command,
            "cancel-experiment",
        )

        managed = runtime.owners.agent_runtime.query_managed_run(running.run_ref)
        assert managed is not None
        assert managed["status"] == "terminated"
        assert managed["cleanup_status"] == "pending"
        assert runtime.owners.agent_runtime.query_active_experiment_run() is None
        assert (
            runtime.owners.agent_runtime.query_snapshot().facts[
                "active_experiment_run_count"
            ]
            == 0
        )
        assert (
            runtime.owners.agent_runtime.query_snapshot().facts["active_run_count"]
            == 0
        )
        execution = runtime.experiment.query(
            admitted["identities"]["evaluation_attempt_ref"]
        )["execution"]
        assert execution["status"] == "failed"
        assert execution["managed_status"] == "terminated"
        assert execution["fence_status"] == "revoked"
        canonical = runtime.owners.agent_runtime.query_experiment_run(
            admitted["identities"]["evaluation_attempt_ref"]
        )
        assert canonical is not None
        assert canonical.failure_code == "runtime_control_cancel"

        # Logical termination releases admission immediately, while physical
        # cleanup remains pending until the actual provider call returns.
        successor = runtime.experiment.start(
            ExperimentIntent(
                execution_request_ref="cancel-experiment-successor",
                quest_ref=quest["quest_ref"],
                title="successor managed experiment",
                hypothesis="终止后 idle admission 必须立即可用。",
                variant_parameter=0.5,
                sample_count=16,
            ),
            "cancel-experiment-successor-start",
            require_idle=True,
        )
        assert successor["execution"]["status"] == "admitted"
        assert successor["execution"]["run_ref"] != running.run_ref

        emit_late_observation.set()
        assert late_observation_returned.wait(timeout=5)
        still_pending = runtime.owners.agent_runtime.query_managed_run(
            running.run_ref
        )
        assert still_pending is not None
        assert still_pending["cleanup_status"] == "pending"
        release.set()
        worker.join(timeout=5)
        assert not worker.is_alive()
        assert worker_errors == []
        cleaned = runtime.owners.agent_runtime.query_managed_run(running.run_ref)
        assert cleaned is not None
        assert cleaned["cleanup_status"] == "completed"
    finally:
        emit_late_observation.set()
        release.set()
        runtime.close()


def test_terminal_provider_cleanup_recovers_after_daemon_restart(
    tmp_path: Path,
) -> None:
    class _CleanupProvider(_DeterministicExperimentProvider):
        def __init__(self) -> None:
            super().__init__()
            self.cleaned: list[str] = []

        def reconcile_cancelled_job(self, job_ref: str) -> bool:
            self.cleaned.append(job_ref)
            return True

    data_root = tmp_path / "terminal-cleanup-restart"
    runtime = _experiment_runtime(data_root, _DeterministicExperimentProvider())
    quest = _confirm_experiment_quest(runtime)
    admitted = runtime.experiment.start(
        ExperimentIntent(
            execution_request_ref="terminal-cleanup-restart-request",
            quest_ref=quest["quest_ref"],
            title="restart cleanup",
            hypothesis="重启后仍按 provider operation 对账物理清理。",
            variant_parameter=0.25,
            sample_count=16,
        ),
        "terminal-cleanup-restart-start",
    )
    running = runtime.owners.agent_runtime.claim_next_experiment()
    assert running is not None
    foreground = runtime.owners.advancement_engine.query_foreground(
        quest["quest_ref"]
    )
    assert foreground is not None
    cancel = _confirmed_control(
        runtime.owners.human_collaboration,
        scope_ref=f"quest:{quest['quest_ref']}",
        payload={
            "action": "cancel",
            "target": {
                "target_scope": "run",
                "quest_ref": quest["quest_ref"],
                "cycle_ref": foreground["cycle_ref"],
                "question_ref": foreground["question_ref"],
                "epoch": foreground["epoch"],
                "run_ref": running.run_ref,
            },
            "reason": "operator_requested",
        },
        key="terminal-cleanup-restart",
    )
    _execute_control(
        runtime.owners.human_collaboration,
        cancel,
        "terminal-cleanup-restart",
    )
    assert runtime.owners.agent_runtime.query_managed_run(running.run_ref)[
        "cleanup_status"
    ] == "pending"
    operation_ref = running.provider_operation_ref
    runtime.close()

    provider = _CleanupProvider()
    restarted = _experiment_runtime(data_root, provider)
    try:
        assert restarted.experiment.process_once() is True
        assert provider.cleaned == [operation_ref]
        cleaned = restarted.owners.agent_runtime.query_managed_run(running.run_ref)
        assert cleaned is not None
        assert cleaned["cleanup_status"] == "completed"
        assert restarted.experiment.query(
            admitted["identities"]["evaluation_attempt_ref"]
        )["execution"]["status"] == "failed"
    finally:
        restarted.close()


def test_run_scoped_experiment_cancel_preserves_formal_run_counters(
    tmp_path: Path,
) -> None:
    runtime = _experiment_runtime(tmp_path / "experiment-count-isolation")
    try:
        quest = _confirm_experiment_quest(runtime)
        question, request, idea_run = _admit_direct_idea_request(
            runtime,
            quest,
            "experiment-count-isolation-idea",
        )
        admitted = runtime.experiment.start(
            ExperimentIntent(
                execution_request_ref="experiment-count-isolation-request",
                quest_ref=quest["quest_ref"],
                title="count isolation",
                hypothesis="Experiment 终止不改变 formal Run 计数。",
                variant_parameter=0.25,
                sample_count=16,
            ),
            "experiment-count-isolation-start",
        )
        experiment_run = runtime.owners.agent_runtime.query_experiment_run(
            admitted["identities"]["evaluation_attempt_ref"]
        )
        assert experiment_run is not None
        before_cancel = runtime.owners.agent_runtime.query_snapshot()
        assert before_cancel.facts["active_run_count"] == 1
        assert before_cancel.facts["active_experiment_run_count"] == 1
        foreground = runtime.owners.advancement_engine.query_foreground(
            quest["quest_ref"]
        )
        assert foreground is not None
        cancel = _confirmed_control(
            runtime.owners.human_collaboration,
            scope_ref=f"quest:{quest['quest_ref']}",
            payload={
                "action": "cancel",
                "target": {
                    "target_scope": "run",
                    "quest_ref": quest["quest_ref"],
                    "cycle_ref": foreground["cycle_ref"],
                    "question_ref": foreground["question_ref"],
                    "epoch": foreground["epoch"],
                    "run_ref": experiment_run.run_ref,
                },
                "reason": "operator_requested",
            },
            key="experiment-count-isolation-cancel",
        )
        _execute_control(
            runtime.owners.human_collaboration,
            cancel,
            "experiment-count-isolation-cancel",
        )
        after_cancel = runtime.owners.agent_runtime.query_snapshot()
        assert after_cancel.facts["active_run_count"] == 1
        assert after_cancel.facts["active_experiment_run_count"] == 0

        outcome = _no_viable_outcome(
            question.question_ref,
            request.context_pack_ref,
        )
        execution = _record_direct_execution(
            runtime,
            run_ref=idea_run.run_ref,
            attempt_ref=idea_run.attempt_ref,
            fence_ref=idea_run.fence_ref,
            submission_ref="experiment-count-isolation-submission",
            native_session_ref="experiment-count-isolation-session",
            runtime_binding=idea_run.runtime_binding,
            outcome=outcome,
            reviewed_draft=outcome,
            review=_review(canonical_hash(outcome)),
            idempotency_key="experiment-count-isolation-execution",
        )
        content = runtime.owners.research_memory.accept_idea_outcome_content(
            request_ref=request.request_ref,
            run_ref=idea_run.run_ref,
            attempt_ref=idea_run.attempt_ref,
            fence_ref=idea_run.fence_ref,
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
        assert decision.outcome_ref is not None
        runtime.owners.agent_runtime.complete_idea_run(
            run_ref=idea_run.run_ref,
            attempt_ref=idea_run.attempt_ref,
            fence_ref=idea_run.fence_ref,
            outcome_ref=decision.outcome_ref,
            decision_receipt=decision.receipt,
            idempotency_key="experiment-count-isolation-complete",
        )
        completed = runtime.owners.agent_runtime.query_snapshot()
        assert completed.revision > after_cancel.revision
        assert completed.facts["active_run_count"] == 0
        assert completed.facts["completed_run_count"] == (
            after_cancel.facts["completed_run_count"] + 1
        )
    finally:
        runtime.close()


def test_cycle_cancel_is_recoverable_without_reopening_terminal_run(
    tmp_path: Path,
) -> None:
    runtime = _runtime(prepare_data_root(tmp_path / "cycle-cancel-resume"), _IdeaProvider())
    try:
        completed = _confirm_question_with_prefix(runtime, "cycle-cancel-resume")
        _root, old_request, old_run = _admit_direct_idea_request(
            runtime, completed, "cycle-cancel-resume"
        )
        outcome = _no_viable_outcome(
            old_request.accepted_question.question_ref,
            old_request.context_pack_ref,
        )
        _record_direct_execution(
            runtime,
            run_ref=old_run.run_ref,
            attempt_ref=old_run.attempt_ref,
            fence_ref=old_run.fence_ref,
            submission_ref="cycle-cancel-resume-submission",
            native_session_ref="cycle-cancel-resume-native",
            runtime_binding=old_run.runtime_binding,
            outcome=outcome,
            reviewed_draft=outcome,
            review=_review(canonical_hash(outcome)),
            idempotency_key="cycle-cancel-resume-execution",
        )
        foreground = runtime.owners.advancement_engine.query_foreground(
            completed["quest_ref"]
        )
        assert foreground is not None
        cancel = _confirmed_control(
            runtime.owners.human_collaboration,
            scope_ref=f"quest:{completed['quest_ref']}",
            payload={
                "action": "cancel",
                "target": {
                    "quest_ref": completed["quest_ref"],
                    "cycle_ref": completed["cycle_ref"],
                    "question_ref": foreground["question_ref"],
                    "epoch": foreground["epoch"],
                },
                "reason": "operator_requested",
            },
            key="cycle-cancel",
        )
        _execute_control(runtime.owners.human_collaboration, cancel, "cycle-cancel")
        cancelled = runtime.owners.advancement_engine.query_foreground(
            completed["quest_ref"]
        )
        assert cancelled is not None
        assert cancelled["status"] == "suspended"
        assert cancelled["grant_status"] == "revoked"
        assert runtime.owners.agent_runtime.query_managed_run(old_run.run_ref)[
            "status"
        ] == "terminated"
        terminated_run = runtime.owners.agent_runtime.query_idea_stage_run(
            old_request.request_ref
        )
        assert terminated_run is not None
        assert terminated_run.execution is not None
        assert _public_run(terminated_run)["fence_status"] == "revoked"

        resume = _confirmed_control(
            runtime.owners.human_collaboration,
            scope_ref=f"quest:{completed['quest_ref']}",
            payload={
                "action": "resume",
                "target": {
                    "quest_ref": completed["quest_ref"],
                    "cycle_ref": completed["cycle_ref"],
                    "question_ref": foreground["question_ref"],
                    "epoch": cancelled["epoch"],
                },
                "reason": "operator_requested",
            },
            key="cycle-resume-after-cancel",
        )
        _execute_control(
            runtime.owners.human_collaboration,
            resume,
            "cycle-resume-after-cancel",
        )
        active = runtime.owners.advancement_engine.query_foreground(
            completed["quest_ref"]
        )
        assert active is not None
        assert active["status"] == "active"
        assert active["epoch"] == foreground["epoch"] + 1

        runtime.idea_stage.start("cycle-resume-new-run")
        new_request = runtime.owners.advancement_engine.query_idea_stage_request(
            completed["cycle_ref"]
        )
        assert new_request is not None
        assert new_request.request_ref != old_request.request_ref
        assert new_request.epoch == active["epoch"]
        new_run = runtime.owners.agent_runtime.query_idea_stage_run(
            new_request.request_ref
        )
        assert new_run is not None
        assert new_run.run_ref != old_run.run_ref
        assert runtime.owners.agent_runtime.query_managed_run(old_run.run_ref)[
            "status"
        ] == "terminated"
    finally:
        runtime.close()


def test_run_scoped_cancel_rejects_formal_stage_run_without_partial_effect(
    tmp_path: Path,
) -> None:
    runtime = _runtime(prepare_data_root(tmp_path / "formal-run-cancel"), _IdeaProvider())
    try:
        completed = _confirm_question_with_prefix(runtime, "formal-run-cancel")
        _root, _request, run = _admit_direct_idea_request(
            runtime, completed, "formal-run-cancel"
        )
        foreground = runtime.owners.advancement_engine.query_foreground(
            completed["quest_ref"]
        )
        assert foreground is not None
        draft = runtime.owners.human_collaboration.create_command_draft(
            f"quest:{completed['quest_ref']}",
            {
                "command_kind": "research_control",
                "payload": {
                    "action": "cancel",
                    "target": {
                        "target_scope": "run",
                        "quest_ref": completed["quest_ref"],
                        "cycle_ref": completed["cycle_ref"],
                        "question_ref": foreground["question_ref"],
                        "epoch": foreground["epoch"],
                        "run_ref": run.run_ref,
                    },
                    "reason": "operator_requested",
                },
            },
            "formal-run-cancel-draft",
        )
        with pytest.raises(
            OwnerConflict, match="formal_stage_run_cancel_requires_stage_scope"
        ):
            runtime.owners.human_collaboration.preview_command(
                draft["intent_id"],
                draft["draft_revision"],
                draft["draft_hash"],
                "formal-run-cancel-preview",
            )
        assert runtime.owners.agent_runtime.query_managed_run(run.run_ref)[
            "status"
        ] == "running"
        assert runtime.owners.advancement_engine.query_foreground(
            completed["quest_ref"]
        )["status"] == "active"
    finally:
        runtime.close()


def test_manual_deepfetch_pause_waits_for_provider_safe_boundary(
    tmp_path: Path,
) -> None:
    started = threading.Event()
    stop_requested = threading.Event()

    class _BlockingDeepFetchProvider(DeterministicManualDeepFetchProvider):
        def __init__(self) -> None:
            super().__init__()
            self.cancelled_jobs: list[str] = []

        def cancel_job(self, job_ref: str) -> None:
            self.cancelled_jobs.append(job_ref)
            stop_requested.set()

        def execute(self, request):
            self.requests.append(request)
            started.set()
            assert stop_requested.wait(timeout=5)
            raise DeepFetchUnavailable("deepfetch_provider_stopped")

    provider = _BlockingDeepFetchProvider()
    runtime = _manual_runtime(
        tmp_path / "deepfetch-control",
        deepfetch_provider=provider,
    )
    try:
        quest_ref, parent_question_ref = _accept_root_question(
            runtime, "deepfetch-control"
        )
        human = runtime.owners.human_collaboration
        seeded = _open_and_confirm_seed(
            human,
            quest_ref=quest_ref,
            parent_question_ref=parent_question_ref,
            key_prefix="deepfetch-control",
            deepfetch_preference="use",
        )
        queued = human.start_manual_creation_deepfetch(
            seeded["context_ref"],
            expected_seed_ref=seeded["seed"]["ref"],
            expected_seed_hash=seeded["seed"]["hash"],
            idempotency_key="deepfetch-control-start",
        )
        request_ref = queued["research_path"]["deepfetch"]["request_ref"]
        worker_errors: list[BaseException] = []

        def run_worker() -> None:
            try:
                runtime.deepfetch.process_once()
            except BaseException as error:  # pragma: no cover - asserted below
                worker_errors.append(error)

        worker = threading.Thread(target=run_worker)
        worker.start()
        assert started.wait(timeout=5)
        deepfetch = runtime.owners.agent_runtime.query_deepfetch_run(request_ref)
        assert deepfetch is not None
        managed = runtime.owners.agent_runtime.query_managed_run(deepfetch.run_ref)
        assert managed is not None
        assert managed["run_kind"] == "deepfetch"
        assert managed["root_session_ref"] == deepfetch.root_session_ref
        assert managed["fence_ref"] == deepfetch.fence_ref

        foreground = runtime.owners.advancement_engine.query_foreground(quest_ref)
        assert foreground is not None
        paused = _confirmed_control(
            human,
            scope_ref=f"quest:{quest_ref}",
            payload={
                "action": "pause",
                "target": {
                    "target_scope": "run",
                    "quest_ref": quest_ref,
                    "cycle_ref": foreground["cycle_ref"],
                    "question_ref": foreground["question_ref"],
                    "epoch": foreground["epoch"],
                    "run_ref": deepfetch.run_ref,
                },
                "reason": "operator_requested",
            },
            key="deepfetch-control-pause",
        )
        pause_finished = threading.Event()
        pause_errors: list[BaseException] = []

        def pause_deepfetch() -> None:
            try:
                _execute_control(human, paused, "deepfetch-control-pause")
            except BaseException as error:  # pragma: no cover - asserted below
                pause_errors.append(error)
            finally:
                pause_finished.set()

        pause_worker = threading.Thread(target=pause_deepfetch)
        pause_worker.start()
        assert stop_requested.wait(timeout=5)
        worker.join(timeout=5)
        pause_worker.join(timeout=5)
        assert not worker.is_alive()
        assert not pause_worker.is_alive()
        assert worker_errors == []
        assert pause_errors == []
        controlled = runtime.owners.agent_runtime.query_managed_run(
            deepfetch.run_ref
        )
        assert controlled is not None
        assert controlled["status"] == "suspended"
        assert controlled["safe_point_ref"]
        assert controlled["attempt_ref"] != deepfetch.attempt_ref
        assert controlled["root_session_ref"] == deepfetch.root_session_ref
        assert controlled["fence_ref"] != deepfetch.fence_ref
        assert deepfetch.provider_operation_ref in provider.cancelled_jobs
        assert controlled["safe_point"]["checkpoint"][
            "provider_operation_ref"
        ] == deepfetch.provider_operation_ref

        # A result from the stopped predecessor operation cannot cross the Safe
        # Point after AR has replaced its technical Attempt/Fence.
        provider_request = provider.requests[0]
        late_result = DeterministicManualDeepFetchProvider.execute(
            provider, provider_request
        )
        result_payload, result_hash = validate_deepfetch_result(
            provider_request, late_result
        )
        owner_request = human.query_deepfetch_request(request_ref)
        assert owner_request is not None
        with pytest.raises(OwnerConflict, match="deepfetch_attempt_fence_stale"):
            runtime.owners.agent_runtime._complete_deepfetch_attempt(
                request=owner_request,
                run_ref=deepfetch.run_ref,
                attempt_ref=deepfetch.attempt_ref,
                generation=deepfetch.attempt_generation,
                fence_ref=deepfetch.fence_ref,
                runtime_binding_hash=deepfetch.runtime_binding_hash,
                result_payload=result_payload,
                result_hash=result_hash,
            )
    finally:
        stop_requested.set()
        runtime.close()


def test_daemon_recovery_keeps_managed_experiment_identity_in_sync(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "managed-experiment-recovery"
    runtime = _experiment_runtime(data_root)
    quest = _confirm_experiment_quest(runtime)
    admitted = runtime.experiment.start(
        ExperimentIntent(
            execution_request_ref="managed-experiment-recovery-request",
            quest_ref=quest["quest_ref"],
            title="recover managed experiment",
            hypothesis="重启只替换技术 Attempt 与 Fence。",
            variant_parameter=0.25,
            sample_count=16,
        ),
        "managed-experiment-recovery-start",
    )
    running = runtime.owners.agent_runtime.claim_next_experiment()
    assert running is not None
    old_attempt_ref = running.attempt_ref
    old_fence_ref = running.fence_ref
    run_ref = admitted["execution"]["run_ref"]
    root_session_ref = running.root_session_ref
    runtime.close()

    restarted = _experiment_runtime(data_root)
    try:
        recovered = restarted.experiment.query(
            admitted["identities"]["evaluation_attempt_ref"]
        )["execution"]
        managed = restarted.owners.agent_runtime.query_managed_run(run_ref)
        assert managed is not None
        assert managed["run_ref"] == run_ref
        assert managed["root_session_ref"] == root_session_ref
        assert managed["attempt_ref"] == recovered["attempt_ref"]
        assert managed["fence_ref"] == recovered["fence_ref"]
        assert managed["attempt_ref"] != old_attempt_ref
        assert managed["fence_ref"] != old_fence_ref
        assert managed["status"] == "running"
    finally:
        restarted.close()


def test_daemon_recovery_replaces_inflight_stage_attempt_and_rejects_old_fence(
    tmp_path: Path,
) -> None:
    data_root = prepare_data_root(tmp_path / "inflight-stage-recovery")
    runtime = _runtime(data_root, _IdeaProvider())
    completed = _confirm_question_with_prefix(runtime, "inflight-stage-recovery")
    runtime.idea_stage.start("inflight-stage-recovery-start")
    request = runtime.owners.advancement_engine.query_idea_stage_request(
        completed["cycle_ref"]
    )
    assert request is not None
    run = runtime.owners.agent_runtime.query_idea_stage_run(request.request_ref)
    assert run is not None
    old_attempt_ref = run.attempt_ref
    old_fence_ref = run.fence_ref
    runtime.owners.agent_runtime.begin_provider_unit(
        unit_ref=run.primary_invocation.invocation_ref,
        run_ref=run.run_ref,
        attempt_ref=old_attempt_ref,
        fence_ref=old_fence_ref,
        unit_kind="idea_primary",
    )
    runtime.close()

    restarted = _runtime(data_root, _IdeaProvider())
    try:
        recovered = restarted.owners.agent_runtime.query_idea_stage_run(
            request.request_ref
        )
        assert recovered is not None
        assert recovered.run_ref == run.run_ref
        assert recovered.root_session_ref == run.root_session_ref
        assert recovered.attempt_ref != old_attempt_ref
        assert recovered.fence_ref != old_fence_ref
        with pytest.raises(OwnerConflict, match="runtime_fence_revoked"):
            restarted.owners.agent_runtime.record_idea_primary_draft(
                run_ref=run.run_ref,
                attempt_ref=old_attempt_ref,
                fence_ref=old_fence_ref,
                native_session_ref="late-old-stage-session",
                runtime_binding=run.runtime_binding,
                draft={"kind": "NoViableCandidate"},
                adapter_kind="test_control",
                idempotency_key="inflight-stage-recovery-late",
            )
    finally:
        restarted.close()


@pytest.mark.parametrize(
    ("action", "expected_status", "replacement_after_apply"),
    [
        ("pause", "suspended", False),
        ("cancel", "terminated", False),
        ("abandon", "terminated", False),
        ("forced_switch", "suspended_fenced", False),
        ("normal_switch", "running", True),
        ("prune", "suspended_fenced", False),
    ],
)
def test_prepared_formal_control_survives_daemon_restart_without_stale_reservation(
    tmp_path: Path,
    action: str,
    expected_status: str,
    replacement_after_apply: bool,
) -> None:
    class _RestartQuiescenceProvider(_IdeaProvider):
        def __init__(self) -> None:
            super().__init__()
            self.cancelled_operations: list[str] = []

        def cancel_job(self, job_ref: str) -> None:
            self.cancelled_operations.append(job_ref)

        def reconcile_cancelled_job(self, job_ref: str) -> bool:
            return job_ref in self.cancelled_operations

    data_root = prepare_data_root(tmp_path / f"prepared-restart-{action}")
    runtime = _runtime(data_root, _RestartQuiescenceProvider())
    completed = _confirm_question_with_prefix(runtime, f"prepared-restart-{action}")
    runtime.idea_stage.start(f"prepared-restart-{action}-start")
    foreground = runtime.owners.advancement_engine.query_foreground(
        completed["quest_ref"]
    )
    assert foreground is not None
    request = runtime.owners.advancement_engine.query_idea_stage_request(
        completed["cycle_ref"]
    )
    assert request is not None
    run = runtime.owners.agent_runtime.query_idea_stage_run(request.request_ref)
    assert run is not None
    runtime.owners.agent_runtime.begin_provider_unit(
        unit_ref=run.primary_invocation.invocation_ref,
        operation_ref=run.primary_invocation.operation_ref,
        run_ref=run.run_ref,
        attempt_ref=run.attempt_ref,
        fence_ref=run.fence_ref,
        unit_kind="idea_primary",
    )
    target = {
        "quest_ref": completed["quest_ref"],
        "cycle_ref": completed["cycle_ref"],
        "question_ref": foreground["question_ref"],
        "epoch": foreground["epoch"],
    }
    if action in {"normal_switch", "forced_switch", "prune"}:
        target["target_question_ref"] = f"{foreground['question_ref']}-target"
    payload = {
        "action": action,
        "target": target,
        "reason": "confirmed_before_daemon_restart",
    }
    affected_question_refs = (
        (str(foreground["question_ref"]),) if action == "prune" else None
    )
    source_stage = "idea" if action == "normal_switch" else None
    _preview, revision = runtime.owners.agent_runtime.preview_runtime_control(
        payload,
        affected_question_refs=affected_question_refs,
        source_stage=source_stage,
    )
    operation_ref = f"prepared-restart-{action}-operation"
    runtime.owners.agent_runtime.prepare_runtime_control(
        operation_ref=operation_ref,
        payload=payload,
        expected_revision=revision,
        idempotency_key=f"prepared-restart-{action}-prepare",
        affected_question_refs=affected_question_refs,
        source_stage=source_stage,
    )
    runtime.close()

    restarted_provider = _RestartQuiescenceProvider()
    restarted = _runtime(data_root, restarted_provider)
    try:
        before_apply = restarted.owners.agent_runtime.query_managed_run(run.run_ref)
        assert before_apply is not None
        assert before_apply["attempt_ref"] == run.attempt_ref
        assert before_apply["fence_ref"] == run.fence_ref

        receipt = restarted.owners.agent_runtime.apply_runtime_control(
            operation_ref=operation_ref,
            payload=payload,
            expected_revision=revision,
            idempotency_key=f"prepared-restart-{action}-apply",
            affected_question_refs=affected_question_refs,
            source_stage=source_stage,
        )
        assert receipt["operation_ref"] == operation_ref
        controlled = restarted.owners.agent_runtime.query_managed_run(run.run_ref)
        assert controlled is not None
        assert controlled["status"] == expected_status
        assert (controlled["attempt_ref"] != run.attempt_ref) is replacement_after_apply
        assert (controlled["fence_ref"] != run.fence_ref) is replacement_after_apply
    finally:
        restarted.close()


def test_reconciliation_pending_provider_blocks_pause_until_terminal_ack(
    tmp_path: Path,
) -> None:
    class _ReconciliationPendingProvider(_IdeaProvider):
        def generate_draft(self, request):
            raise IdeaSkillUnavailable("codex_operation_reconciliation_pending")

    data_root = prepare_data_root(tmp_path / "pending-provider-quiescence")
    runtime = _runtime(data_root, _IdeaProvider())
    completed = _confirm_question_with_prefix(runtime, "pending-provider-quiescence")
    runtime.idea_stage.start("pending-provider-quiescence-start")
    request = runtime.owners.advancement_engine.query_idea_stage_request(
        completed["cycle_ref"]
    )
    assert request is not None
    run = runtime.owners.agent_runtime.query_idea_stage_run(request.request_ref)
    assert run is not None
    runtime.owners.agent_runtime.begin_provider_unit(
        unit_ref=run.primary_invocation.invocation_ref,
        operation_ref=run.primary_invocation.operation_ref,
        run_ref=run.run_ref,
        attempt_ref=run.attempt_ref,
        fence_ref=run.fence_ref,
        unit_kind="idea_primary",
    )
    runtime.close()

    restarted = _runtime(data_root, _ReconciliationPendingProvider())
    try:
        assert restarted.idea_stage.process_once() is False
        recovered = restarted.owners.agent_runtime.query_idea_stage_run(
            request.request_ref
        )
        assert recovered is not None
        with restarted._database.read() as connection:
            units = connection.execute(
                text(
                    "SELECT * FROM ar_provider_units WHERE run_ref = :run_ref ORDER "
                    "BY started_at, unit_ref"
                ),
                {"run_ref": run.run_ref},
            ).all()
        assert [unit.status for unit in units] == ["revocation_pending", "active"]
        assert len({unit.operation_ref for unit in units}) == 1

        foreground = restarted.owners.advancement_engine.query_foreground(
            completed["quest_ref"]
        )
        assert foreground is not None
        pause = _confirmed_control(
            restarted.owners.human_collaboration,
            scope_ref=f"quest:{completed['quest_ref']}",
            payload={
                "action": "pause",
                "target": {
                    "quest_ref": completed["quest_ref"],
                    "cycle_ref": foreground["cycle_ref"],
                    "question_ref": foreground["question_ref"],
                    "epoch": foreground["epoch"],
                },
                "reason": "operator_requested",
            },
            key="pending-provider-pause",
        )
        finished = threading.Event()
        errors: list[BaseException] = []

        def execute_pause() -> None:
            try:
                _execute_control(
                    restarted.owners.human_collaboration,
                    pause,
                    "pending-provider-pause",
                )
            except BaseException as error:  # pragma: no cover - asserted below
                errors.append(error)
            finally:
                finished.set()

        worker = threading.Thread(target=execute_pause)
        worker.start()
        assert not finished.wait(timeout=0.15)
        current_unit = units[-1]
        restarted.owners.agent_runtime.acknowledge_provider_safe_point(
            unit_ref=current_unit.unit_ref,
            run_ref=current_unit.run_ref,
            attempt_ref=current_unit.attempt_ref,
            fence_ref=current_unit.fence_ref,
        )
        worker.join(timeout=5)
        assert finished.is_set()
        assert errors == []
        with restarted._database.read() as connection:
            statuses = connection.execute(
                text(
                    "SELECT status FROM ar_provider_units WHERE run_ref = :run_ref "
                    "ORDER BY started_at, unit_ref"
                ),
                {"run_ref": run.run_ref},
            ).scalars().all()
        assert statuses == ["revoked", "completed"]
    finally:
        restarted.close()


def test_0013_backfills_existing_foreground_cycle_without_identity_rewrite(
    tmp_path: Path,
) -> None:
    data_root = prepare_data_root(tmp_path / "upgrade-0012-control")
    _upgrade_to_revision(data_root.database, "0012_experiment_measurement")
    with sqlite3.connect(data_root.database) as connection:
        connection.execute(
            "INSERT INTO ae_initial_cycles (cycle_ref, initialization_id, "
            "quest_ref, question_ref, question_receipt_ref, "
            "question_receipt_hash, quest_receipt_ref, quest_receipt_hash, "
            "receipt_ref, receipt_hash, activated_at) VALUES (?, ?, ?, ?, ?, ?, "
            "?, ?, ?, ?, ?)",
            (
                "cycle-existing",
                "initialization-existing",
                "quest-existing",
                "question-existing",
                "question-receipt-existing",
                "1" * 64,
                "quest-receipt-existing",
                "2" * 64,
                "cycle-receipt-existing",
                "3" * 64,
                1_720_000_000.0,
            ),
        )
        connection.commit()

    upgrade_database(data_root.database)

    with sqlite3.connect(data_root.database) as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone() == ("0013_advancement_runtime_control",)
        provider_columns = {
            row[1]: row[3]
            for row in connection.execute(
                "PRAGMA table_info(ar_stage_provider_invocations)"
            )
        }
        assert provider_columns["operation_ref"] == 1
        assert connection.execute(
            "SELECT cycle_ref, question_ref, status FROM ae_cycles WHERE "
            "quest_ref = 'quest-existing'"
        ).fetchone() == (
            "cycle-existing",
            "question-existing",
            "ongoing",
        )
        assert connection.execute(
            "SELECT cycle_ref, question_ref, epoch, status FROM "
            "ae_foreground_heads WHERE quest_ref = 'quest-existing'"
        ).fetchone() == (
            "cycle-existing",
            "question-existing",
            1,
            "active",
        )
        assert connection.execute(
            "SELECT cycle_ref, question_ref, epoch, status FROM "
            "ae_foreground_grants WHERE quest_ref = 'quest-existing'"
        ).fetchone() == (
            "cycle-existing",
            "question-existing",
            1,
            "active",
        )
        for table in (
            "ae_cycles",
            "ae_stage_run_requests",
            "ae_stage_commits",
            "ar_stage_runs",
            "ar_stage_run_rebindings",
        ):
            schema = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
                (table,),
            ).fetchone()[0]
            assert "'bundle'" in schema
            assert "'reasoning'" in schema
            assert "'target'" not in schema
            assert "'writing'" not in schema
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)


def test_0013_rebuilds_the_current_stage_from_existing_commits(
    tmp_path: Path,
) -> None:
    data_root = prepare_data_root(tmp_path / "upgrade-current-stage")
    _upgrade_to_revision(data_root.database, "0008_quest_acquisition_session")
    with sqlite3.connect(data_root.database) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        _seed_completed_idea_chain(connection)
    _upgrade_to_revision(data_root.database, "0012_experiment_measurement")

    upgrade_database(data_root.database)

    with sqlite3.connect(data_root.database) as connection:
        assert connection.execute(
            "SELECT stage FROM ae_foreground_heads WHERE cycle_ref = 'cycle-idea'"
        ).fetchone() == ("plan",)
        assert connection.execute(
            "SELECT stage FROM ae_foreground_grants WHERE cycle_ref = 'cycle-idea'"
        ).fetchone() == ("plan",)
        assert connection.execute(
            "SELECT invocation_ref, operation_ref FROM "
            "ar_stage_provider_invocations ORDER BY invocation_ref"
        ).fetchall() == [
            ("invocation-idea-primary", "invocation-idea-primary"),
            ("invocation-idea-review", "invocation-idea-review"),
        ]
