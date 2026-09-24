"""Inbox acknowledgements must track new research facts, not read frequency."""

from __future__ import annotations

from pathlib import Path

import pytest

from meta_research.owners.common import OwnerConflict
from meta_research.target_run_finalizer import TargetRunFinalizer
from test_target_root_finalizer import _EvidenceReader, _root_finalizer_fixture


def test_reading_drained_inbox_preserves_checkpoint_after_real_target_completion(
    tmp_path: Path,
) -> None:
    runtime, lifecycle, memory, _authority, handle, _workspace, evidence = (
        _root_finalizer_fixture(tmp_path)
    )
    try:
        request_ref = runtime.bundle_stage.query_current()["stage_run_request"][
            "request_ref"
        ]
        owner = runtime.owners.agent_runtime
        run = owner.query_bundle_stage_run(request_ref)
        assert run is not None
        scope = dict(
            run_ref=run.run_ref,
            attempt_ref=run.attempt_ref,
            fence_ref=run.fence_ref,
        )
        initial_batch = owner.read_bundle_inbox(**scope)
        initial_checkpoint = owner.acknowledge_bundle_inbox(
            **scope,
            batch=initial_batch,
            idempotency_key="checkpoint-refresh:before-completion",
        )
        finalizer = TargetRunFinalizer(
            lifecycle=lifecycle,
            memory=memory,
            workspace_resolver=runtime.target_run_authorities.agent_runtime,
            evidence_reader=_EvidenceReader(evidence),
            measurement_authority=runtime.owners.research_graph,
            graph_authority=runtime.owners.research_graph,
        )
        completed = finalizer.finalize(handle=handle, evidence=evidence)
        assert completed.status == "completed"
        owner.publish_target_root_completion(
            target_ref=handle.target_ref,
            completion_ref=completed.completion_ref,
            target_commit_ref=completed.target_commit_ref,
        )
        with pytest.raises(OwnerConflict, match="bundle_inbox_checkpoint_stale"):
            owner.verify_bundle_inbox_checkpoint(
                **scope,
                checkpoint=initial_checkpoint,
                require_current=True,
            )
        first_batch = owner.read_bundle_inbox(**scope)
        assert len(first_batch.notices) == 1
        checkpoint = owner.acknowledge_bundle_inbox(
            **scope,
            batch=first_batch,
            idempotency_key="checkpoint-refresh:consume-completion",
        )

        # The Bundle worker and its resident root can independently reread the
        # same drained inbox while a dispatch/proposal request is in flight.
        empty_batch = owner.read_bundle_inbox(**scope)
        assert empty_batch.notices == ()
        assert empty_batch.next_cursor == checkpoint.cursor
        assert empty_batch.generation == checkpoint.generation
        repeated = owner.acknowledge_bundle_inbox(
            **scope,
            batch=empty_batch,
            idempotency_key="checkpoint-refresh:observe-drained",
        )
        graph = runtime.owners.research_graph.query_target_graph(request_ref)
        assert graph is not None
        assert not graph.strategy_complete
        proposal_command = dict(
            **scope,
            native_session_ref=run.native_session_ref,
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
            inbox_checkpoint=checkpoint,
            idempotency_key="checkpoint-refresh:proposal-after-observation",
        )
        proposal = owner.record_bundle_target_proposal(**proposal_command)
        assert owner.record_bundle_target_proposal(**proposal_command) == proposal
        owner.verify_bundle_inbox_checkpoint(
            **scope,
            checkpoint=checkpoint,
            require_current=True,
        )
        assert repeated == checkpoint
        assert owner.query_bundle_inbox_checkpoint(run.run_ref) == checkpoint

        # Both Owner idempotency replay and another observer must retain the
        # accepted receipt rather than allocate an equivalent checkpoint.
        assert owner.acknowledge_bundle_inbox(
            **scope,
            batch=first_batch,
            idempotency_key="checkpoint-refresh:consume-completion",
        ) == checkpoint
        assert owner.acknowledge_bundle_inbox(
            **scope,
            batch=owner.read_bundle_inbox(**scope),
            idempotency_key="checkpoint-refresh:another-observer",
        ) == checkpoint

        # Continue through the public worker boundary. It must consume this
        # exact proposal after its own additional inbox reads, without asking
        # the provider to replace a decision made from the same research facts.
        for _step in range(8):
            runtime.bundle_stage.process_once()
            current = runtime.owners.research_graph.query_target_graph(request_ref)
            assert current is not None
            if current.strategy_complete:
                break
        else:
            raise AssertionError("Bundle did not accept the unchanged-input proposal")
        assert owner.query_bundle_target_proposals(run.run_ref) == (proposal,)
        assert owner.record_bundle_target_proposal(**proposal_command) == proposal
    finally:
        runtime.close()
