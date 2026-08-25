from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from meta_research.bundle_protocol import TargetWorkNotice, projection_plain_value
from meta_research.owners.common import OwnerConflict, canonical_hash, canonical_json
from test_public_bundle_stage import (
    _DeterministicBundleSkill,
    _bundle_runtime,
    _confirm_direct_quest,
    _finish_idea_stage,
    _finish_plan_stage,
)


class _DispatchCountingSkill(_DeterministicBundleSkill):
    def __init__(self) -> None:
        self.schedule_calls = 0

    def schedule_target(self, request):
        self.schedule_calls += 1
        return super().schedule_target(request)


def _notice(*, target_ref: str, sequence: int, suffix: str) -> TargetWorkNotice:
    payload = {
        "notice_ref": f"target-work-notice:{suffix}",
        "terminal_transition_ref": f"target-terminal-transition:{suffix}",
        "kind": "coordination_required",
        "target_ref": target_ref,
        "target_run_ref": f"target-run:{suffix}",
        "execution_attempt_ref": f"target-attempt:{suffix}",
        "execution_fence_ref": f"target-fence:{suffix}",
        "terminal_fact_ref": f"target-blocker:{suffix}",
        "handoff_manifest_ref": f"target-handoff:{suffix}",
        "handoff_manifest_sha256": "a" * 64,
        "compact_reason": "terminal frontier deliberately unavailable",
        "pending_obligation_refs": (f"obligation:{suffix}",),
    }
    return TargetWorkNotice(
        **payload,
        sequence=sequence,
        payload_sha256=canonical_hash(payload),
    )


def _insert_unbacked_notice(
    connection: sqlite3.Connection,
    *,
    run_ref: str,
    notice: TargetWorkNotice,
    scoped_sequence: int,
    generation: int,
) -> None:
    notice_value = projection_plain_value(notice)
    connection.execute(
        "INSERT INTO ar_target_work_notices (notice_ref, sequence, "
        "terminal_transition_ref, target_ref, manifest_ref, kind, notice_json, "
        "notice_hash, idempotency_key, request_hash, published_at) VALUES "
        "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1.0)",
        (
            notice.notice_ref,
            notice.sequence,
            notice.terminal_transition_ref,
            notice.target_ref,
            notice.handoff_manifest_ref,
            notice.kind,
            canonical_json(notice_value),
            canonical_hash(notice_value),
            f"publish:{notice.notice_ref}",
            canonical_hash({"notice_ref": notice.notice_ref}),
        ),
    )
    connection.execute(
        "INSERT INTO ar_bundle_inbox_entries (run_ref, sequence, notice_ref, "
        "published_generation, published_at) VALUES (?, ?, ?, ?, 1.0)",
        (run_ref, scoped_sequence, notice.notice_ref, generation),
    )
    connection.execute(
        "UPDATE ar_bundle_inbox_scopes SET next_sequence = ?, generation = ?, "
        "wake_pending = 1, current_checkpoint_ref = NULL, updated_at = 1.0 "
        "WHERE run_ref = ?",
        (scoped_sequence + 1, generation, run_ref),
    )


def test_scoped_inbox_isolates_runs_and_missing_frontier_cannot_redispatch(
    tmp_path: Path,
) -> None:
    provider = _DispatchCountingSkill()
    runtime = _bundle_runtime(
        tmp_path / "bundle-inbox-missing-frontier",
        bundle_skill_provider=provider,
    )
    try:
        _confirm_direct_quest(runtime)
        _finish_idea_stage(runtime)
        _finish_plan_stage(runtime)
        for _step in range(20):
            runtime.bundle_stage.process_once()
            current = runtime.bundle_stage.query_current()
            graph_value = current["target_graph"]
            if graph_value["status"] != "accepted":
                continue
            target_ref = graph_value["targets"][0]["target_ref"]
            request_ref = current["stage_run_request"]["request_ref"]
            run = runtime.owners.agent_runtime.query_bundle_stage_run(request_ref)
            assert run is not None
            if runtime.owners.agent_runtime.query_bundle_dispatch_decisions(
                run.run_ref
            ):
                break
        else:
            raise AssertionError("Bundle did not accept the first dispatch")

        first_decisions = runtime.owners.agent_runtime.query_bundle_dispatch_decisions(
            run.run_ref
        )
        assert len(first_decisions) == 1
        assert provider.schedule_calls == 1
        assert runtime.owners.agent_runtime.query_target_launch_ack(target_ref) is None
        graph = runtime.owners.research_graph.query_target_graph(request_ref)
        assert graph is not None
        stale_checkpoint = (
            runtime.owners.agent_runtime.query_bundle_inbox_operation_checkpoint(
                operation_kind="dispatch",
                operation_ref=first_decisions[0].decision_ref,
            )
        )
        assert stale_checkpoint is not None

        other_run_ref = "bundle-run:isolated"
        other_attempt_ref = "bundle-attempt:isolated"
        other_fence_ref = "bundle-fence:isolated"
        first_notice = _notice(target_ref=target_ref, sequence=1, suffix="first")
        other_notice = _notice(
            target_ref="target:isolated",
            sequence=2,
            suffix="isolated",
        )
        with sqlite3.connect(runtime.data_root.database) as connection:
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.execute(
                "INSERT INTO ar_stage_runs (run_ref, request_ref, cycle_ref, stage, "
                "epoch, context_pack_ref, context_pack_hash, runtime_binding_json, "
                "runtime_binding_hash, request_receipt_ref, request_receipt_hash, "
                "status, current_attempt_ref, root_session_ref, current_fence_ref, "
                "completion_receipt_ref, completion_receipt_hash, outcome_ref, "
                "admission_key, admission_hash, created_at, updated_at) VALUES "
                "(?, 'stage-request:isolated', 'cycle:isolated', 'bundle', 1, "
                "'context:isolated', ?, '{}', ?, 'request-receipt:isolated', ?, "
                "'running', ?, 'root-session:isolated', ?, NULL, NULL, NULL, "
                "'admit:isolated', ?, 1.0, 1.0)",
                (
                    other_run_ref,
                    "1" * 64,
                    canonical_hash({}),
                    "2" * 64,
                    other_attempt_ref,
                    other_fence_ref,
                    "3" * 64,
                ),
            )
            connection.execute(
                "INSERT INTO ar_bundle_inbox_scopes (run_ref, next_sequence, "
                "generation, wake_pending, acknowledged_cursor, "
                "current_checkpoint_ref, updated_at) VALUES (?, 1, 0, 0, 0, "
                "NULL, 1.0)",
                (other_run_ref,),
            )
            _insert_unbacked_notice(
                connection,
                run_ref=run.run_ref,
                notice=first_notice,
                scoped_sequence=1,
                generation=1,
            )
            _insert_unbacked_notice(
                connection,
                run_ref=other_run_ref,
                notice=other_notice,
                scoped_sequence=1,
                generation=1,
            )
            connection.execute(
                "UPDATE ar_bundle_inbox_state SET next_sequence = 3, "
                "generation = 1, wake_pending = 1 WHERE singleton = 'bundle'"
            )

        first_batch = runtime.owners.agent_runtime.read_bundle_inbox(
            run_ref=run.run_ref,
            attempt_ref=run.attempt_ref,
            fence_ref=run.fence_ref,
        )
        other_batch = runtime.owners.agent_runtime.read_bundle_inbox(
            run_ref=other_run_ref,
            attempt_ref=other_attempt_ref,
            fence_ref=other_fence_ref,
        )
        assert tuple(item.target_ref for item in first_batch.notices) == (target_ref,)
        assert tuple(item.target_ref for item in other_batch.notices) == (
            "target:isolated",
        )
        assert first_batch.notices[0].sequence == 1
        assert other_batch.notices[0].sequence == 1

        with pytest.raises(
            OwnerConflict,
            match="bundle_inbox_checkpoint_stale",
        ):
            runtime.owners.agent_runtime.record_bundle_target_proposal(
                run_ref=run.run_ref,
                attempt_ref=run.attempt_ref,
                fence_ref=run.fence_ref,
                native_session_ref=first_decisions[0].native_session_ref,
                graph_ref=graph.graph_ref,
                base_generation=graph.head_generation,
                base_head_receipt=graph.head_receipt,
                strategy_update={
                    "schema_ref": graph.target_plan["initial_strategy_update"][
                        "schema_ref"
                    ],
                    "revision": graph.head_generation + 2,
                    "candidates": [],
                    "requires_accepted_labels": [],
                    "strategy_complete": True,
                },
                inbox_checkpoint=stale_checkpoint,
                idempotency_key="stale-inbox-target-proposal",
            )
        assert runtime.owners.agent_runtime.query_bundle_target_proposals(
            run.run_ref
        ) == ()

        with pytest.raises(
            OwnerConflict,
            match="bundle_inbox_checkpoint_stale",
        ):
            runtime.owners.agent_runtime.record_bundle_dispatch_decision(
                run_ref=run.run_ref,
                attempt_ref=run.attempt_ref,
                fence_ref=run.fence_ref,
                native_session_ref=first_decisions[0].native_session_ref,
                graph_ref=graph.graph_ref,
                generation=2,
                frontier=first_decisions[0].frontier,
                state=first_decisions[0].state,
                action=first_decisions[0].action,
                selected_target_ref=first_decisions[0].selected_target_ref,
                rationale="This stale decision must not be persisted.",
                inbox_checkpoint=stale_checkpoint,
                idempotency_key="stale-inbox-target-dispatch",
            )
        assert runtime.owners.agent_runtime.query_bundle_dispatch_decisions(
            run.run_ref
        ) == first_decisions

        runtime.owners.research_graph.accept_target_candidate_projection(
            target_ref=target_ref,
            idempotency_key="stale-inbox-candidate-projection",
        )
        launch_request = runtime.owners.research_graph.query_target_launch_request(
            target_ref
        )
        with pytest.raises(
            OwnerConflict,
            match="bundle_inbox_checkpoint_stale",
        ):
            runtime.owners.agent_runtime.admit_target_launch(
                launch_request,
                dispatch_decision_ref=first_decisions[0].decision_ref,
                idempotency_key="stale-inbox-target-launch",
            )
        assert runtime.owners.agent_runtime.query_target_launch_ack(target_ref) is None

        with pytest.raises(
            OwnerConflict,
            match="bundle_inbox_notice_frontier_invalid",
        ):
            runtime.bundle_stage.process_once()
        assert provider.schedule_calls == 1
        assert runtime.owners.agent_runtime.query_bundle_dispatch_decisions(
            run.run_ref
        ) == first_decisions
        assert runtime.owners.agent_runtime.query_bundle_inbox_checkpoint(
            run.run_ref
        ) is None
    finally:
        runtime.close()
