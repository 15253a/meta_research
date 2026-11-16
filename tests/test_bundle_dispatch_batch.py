import sqlite3
from dataclasses import replace

import pytest
from sqlalchemy.exc import IntegrityError

from meta_research.bundle_skill import BundleDispatchResult
from meta_research.owners.agent_runtime import TargetLaunchAuthorization
from meta_research.owners.common import OwnerConflict, canonical_json
from test_bundle_inbox_runtime import _insert_unbacked_notice, _notice
from test_public_bundle_stage import (
    _HighRiskBundleSkill,
    _ParallelTwoTargetBundleSkill,
    _bundle_runtime,
    _confirm_direct_quest,
    _finalize_target_root,
    _finish_idea_stage,
    _finish_plan_stage,
    _grant_request_capability,
    _open_root_target_authorization_request,
)
from test_repeated_target_authorization import _decide


class _BatchBundleSkill(_ParallelTwoTargetBundleSkill):
    def __init__(self):
        self.dispatch_requests = []
        self.batch_requests = []

    def schedule_targets(self, request):
        self.dispatch_requests.append(request)
        refs = tuple(item["target_ref"] for item in request.frontier)
        if any(item.get("dispatch_allowed") is False for item in request.frontier):
            refs = ()
        return BundleDispatchResult(
            action="dispatch" if refs else "wait",
            selected_target_refs=refs,
            rationale="Run both comparisons once their launch authorities are ready.",
            native_session_ref=request.native_session_ref,
            adapter_kind="test_batch",
        )

    def propose_target_batch(self, request):
        self.batch_requests.append(request)
        return super().propose_target_batch(request)


class _MixedRiskBatchSkill(_HighRiskBundleSkill, _BatchBundleSkill):
    def schedule_targets(self, request):
        result = super().schedule_targets(request)
        return replace(result, selected_target_refs=result.selected_target_refs[::-1])


def _prepare(runtime):
    _confirm_direct_quest(runtime)
    _finish_idea_stage(runtime)
    _finish_plan_stage(runtime)


def _record_batch(runtime):
    for _ in range(16):
        runtime.bundle_stage.process_once()
        request_ref = runtime.bundle_stage.query_current()["stage_run_request"]["request_ref"]
        owner = runtime.owners.agent_runtime
        run = owner.query_bundle_stage_run(request_ref)
        if run is None:
            continue
        decisions = owner.query_bundle_dispatch_decisions(run.run_ref)
        if decisions and decisions[-1].action == "dispatch":
            graph = runtime.owners.research_graph.query_target_graph(request_ref)
            assert graph is not None
            assert set(decisions[-1].selected_target_refs) == {target.target_ref for target in graph.targets}
            assert len(decisions[-1].selected_target_refs) == 2
            return graph, decisions[-1]
    raise AssertionError("Bundle did not record a two-Target dispatch decision")


def test_bundle_admits_two_independent_targets_from_one_dispatch(tmp_path):
    provider = _BatchBundleSkill()
    runtime = _bundle_runtime(tmp_path, bundle_skill_provider=provider)
    try:
        _prepare(runtime)
        graph, decision = _record_batch(runtime)
        agent_runtime = runtime.owners.agent_runtime
        research_graph = runtime.owners.research_graph
        refs = decision.selected_target_refs
        assert refs == tuple(target.target_ref for target in graph.targets)
        assert tuple(agent_runtime.query_target_launch_ack(ref) for ref in refs) == (None, None)
        assert runtime.bundle_stage.process_once()

        launches = tuple(agent_runtime.query_admitted_target_launch(ref) for ref in refs)
        assert all(launch is not None for launch in launches), launches
        assert tuple(launch.request.target_ref for launch in launches) == refs
        assert {launch.dispatch_decision_ref for launch in launches} == {decision.decision_ref}
        assert len({launch.receipt.receipt_ref for launch in launches}) == 2
        assert len({launch.target_run_ref for launch in launches}) == 2
        assert agent_runtime.query_bundle_dispatch_decisions(decision.run_ref) == (decision,)
        assert len(provider.dispatch_requests) == 1

        runtime.configure_resident_mcp_endpoint("http://127.0.0.1:8999")
        for ref in refs:
            for _ in range(8):
                if agent_runtime.query_target_frontier_entry(ref) is not None:
                    break
                assert runtime.target_run_runtime.process_once(ref), runtime.target_run_runtime.query_status(ref)
            else:
                raise AssertionError(f"Target root did not activate for {ref}")
        first, second = (agent_runtime.query_target_frontier_entry(ref) for ref in refs)
        assert first.state == second.state == "running"
        assert first.current_handle.root_session_ref != second.current_handle.root_session_ref
        assert tuple(entry.current_handle.target_run_ref for entry in (first, second)) == tuple(
            launch.target_run_ref for launch in launches
        )
        assert research_graph.query_target_commits(graph.graph_ref) == ()

        first_commit = _finalize_target_root(runtime, launches[0], first.current_handle, 1)
        assert tuple(item.commit_ref for item in research_graph.query_target_commits(graph.graph_ref)) == (
            first_commit,
        )
        for _ in range(4):
            if not runtime.bundle_stage.process_once():
                break
        else:
            raise AssertionError("Bundle did not settle while the second root was running")
        assert provider.batch_requests == []
        assert agent_runtime.query_target_frontier_entry(refs[1]).state == "running"

        second_commit = _finalize_target_root(runtime, launches[1], second.current_handle, 1)
        for _ in range(4):
            runtime.bundle_stage.process_once()
            if provider.batch_requests:
                break
        request, = provider.batch_requests
        assert {entry["commit_ref"] for entry in request.target_commits} == {first_commit, second_commit}
        assert len(provider.dispatch_requests) == 1
    finally:
        runtime.close()


def test_admission_interruption_rolls_back_launches_and_reuses_decision(tmp_path):
    provider = _BatchBundleSkill()
    runtime = _bundle_runtime(tmp_path, bundle_skill_provider=provider)
    try:
        _prepare(runtime)
        graph, decision = _record_batch(runtime)
        database = runtime.data_root.database
        refs = decision.selected_target_refs
        with sqlite3.connect(database) as connection:
            connection.execute("""
                CREATE TRIGGER interrupt_second_launch BEFORE INSERT ON ar_target_launches
                WHEN (SELECT count(*) FROM ar_target_launches) = 1
                BEGIN SELECT RAISE(ABORT, 'second launch interrupted'); END
            """)
        with pytest.raises(IntegrityError, match="second launch interrupted"):
            runtime.bundle_stage.process_once()
        assert tuple(runtime.owners.agent_runtime.query_target_launch_ack(ref) for ref in refs) == (None, None)
        with sqlite3.connect(database) as connection:
            connection.execute("DROP TRIGGER interrupt_second_launch")
        runtime.close()

        runtime = _bundle_runtime(tmp_path, bundle_skill_provider=provider)
        assert runtime.bundle_stage.process_once()
        owner = runtime.owners.agent_runtime
        launches = tuple(owner.query_admitted_target_launch(ref) for ref in refs)
        assert tuple(launch.dispatch_decision_ref for launch in launches) == (decision.decision_ref,) * 2
        runtime.close()

        runtime = _bundle_runtime(tmp_path, bundle_skill_provider=provider)
        assert not runtime.bundle_stage.process_once()
        assert runtime.bundle_stage.observation.code == "target_launch_pending"
        owner = runtime.owners.agent_runtime
        assert tuple(owner.query_admitted_target_launch(ref) for ref in refs) == launches
        assert owner.query_bundle_dispatch_decisions(decision.run_ref) == (decision,)
        replay = owner.settle_bundle_dispatch_launches(decision.decision_ref, authorizations={})
        assert replay == ()
        assert tuple(owner.query_admitted_target_launch(ref) for ref in refs) == launches
        assert runtime.owners.research_graph.query_target_commits(graph.graph_ref) == ()
        assert len(provider.dispatch_requests) == 1
    finally:
        runtime.close()


@pytest.mark.parametrize("revoked", [False, True])
def test_one_high_risk_member_blocks_every_launch_until_all_are_authorized(tmp_path, revoked):
    provider = _MixedRiskBatchSkill()
    runtime = _bundle_runtime(tmp_path, bundle_skill_provider=provider)
    try:
        _prepare(runtime)
        for _ in range(16):
            runtime.bundle_stage.process_once()
            if provider.dispatch_requests:
                break
        authorization_request = _open_root_target_authorization_request(runtime, provider.dispatch_requests[0])
        grant = _grant_request_capability(runtime, authorization_request)
        graph, decision = _record_batch(runtime)
        owner = runtime.owners.agent_runtime
        run = owner.query_bundle_stage_run(graph.request_ref)
        normal, high = decision.selected_target_refs
        assert graph.targets[0].target_ref == high
        assert graph.targets[0].spec["risk_class"] == "high"
        waiter = owner.query_human_request(authorization_request["request_ref"])["direct_waiters"][0]
        assert waiter["status"] == "consumed"
        dispatch_requests = tuple(provider.dispatch_requests)
        direct_request = runtime.owners.research_graph.query_target_launch_request(normal)
        with pytest.raises(OwnerConflict, match="target_launch_batch_settlement_required"):
            owner.admit_target_launch(
                direct_request, dispatch_decision_ref=decision.decision_ref,
                idempotency_key="single-member-write-for-a-plural-dispatch",
            )
        authorizations = {}
        code = "target_launch_authorization_invalid"
        if revoked:
            _decide(
                runtime.owners.human_collaboration, authorization_request["required_authorization"],
                "revoked", "batch-revocation", scope_ref=grant["scope_ref"],
            )
            authorizations = {
                high: TargetLaunchAuthorization(
                    authorization_request["request_ref"], f"root_run:{run.run_ref}",
                    run.attempt_generation, grant["receipt_ref"],
                )
            }
            code = "capability_authorization_receipt_invalid"
        with pytest.raises(OwnerConflict, match=f"^{code}$"):
            owner.settle_bundle_dispatch_launches(decision.decision_ref, authorizations=authorizations)
        assert tuple(owner.query_target_launch_ack(ref) for ref in (normal, high)) == (None, None)
        assert owner.query_human_request(authorization_request["request_ref"])["direct_waiters"][0] == waiter

        if revoked:
            assert not runtime.bundle_stage.process_once()
            assert runtime.bundle_stage.observation.kind == "waiting"
            assert tuple(owner.query_target_launch_ack(ref) for ref in (normal, high)) == (None, None)
        else:
            assert runtime.bundle_stage.process_once()
            launches = tuple(owner.query_admitted_target_launch(ref) for ref in (normal, high))
            assert tuple(launch.dispatch_decision_ref for launch in launches) == (decision.decision_ref,) * 2
        assert tuple(provider.dispatch_requests) == dispatch_requests
    finally:
        runtime.close()


def test_batch_settlement_rejects_a_superseded_inbox_cut(tmp_path):
    provider = _BatchBundleSkill()
    runtime = _bundle_runtime(tmp_path, bundle_skill_provider=provider)
    try:
        _prepare(runtime)
        _graph, decision = _record_batch(runtime)
        owner = runtime.owners.agent_runtime
        ref = decision.selected_target_refs[0]
        with sqlite3.connect(runtime.data_root.database) as connection:
            _insert_unbacked_notice(
                connection, run_ref=decision.run_ref,
                notice=_notice(target_ref=ref, sequence=1, suffix="batch-stale"),
                scoped_sequence=1, generation=1,
            )

        with pytest.raises(OwnerConflict, match="bundle_inbox_checkpoint_stale"):
            owner.settle_bundle_dispatch_launches(decision.decision_ref, authorizations={})
        assert tuple(owner.query_target_launch_ack(ref) for ref in decision.selected_target_refs) == (None, None)
        assert owner.query_bundle_dispatch_decisions(decision.run_ref) == (decision,)
    finally:
        runtime.close()


def test_dispatch_receipt_binds_only_a_unique_launchable_selection(tmp_path):
    runtime = _bundle_runtime(tmp_path, bundle_skill_provider=_BatchBundleSkill())
    try:
        _prepare(runtime)
        _graph, decision = _record_batch(runtime)
        owner = runtime.owners.agent_runtime
        first, second = decision.selected_target_refs
        checkpoint = owner.query_bundle_inbox_checkpoint(decision.run_ref)
        cases = (
            ("dispatch", (first, first), decision.frontier, decision.state, "bundle_dispatch_target_not_in_frontier"),
            ("dispatch", (), decision.frontier, decision.state, "bundle_dispatch_target_not_in_frontier"),
            ("dispatch", ("target-outside-frontier",), decision.frontier, decision.state, "bundle_dispatch_target_not_in_frontier"),
            ("wait", (first,), decision.frontier, decision.state, "bundle_dispatch_target_not_in_frontier"),
            ("dispatch", (first,), ({**decision.frontier[0], "dispatch_allowed": False}, decision.frontier[1]), decision.state, "bundle_dispatch_target_not_in_frontier"),
            ("dispatch", (second,), decision.frontier, {**decision.state, "launch_input_errors": {second: "target_input_asset_unavailable"}}, "target_input_asset_unavailable"),
        )
        for action, refs, frontier, state, code in cases:
            with pytest.raises(OwnerConflict, match=f"^{code}$"):
                owner.record_bundle_dispatch_decision(
                    run_ref=decision.run_ref, attempt_ref=decision.attempt_ref,
                    fence_ref=decision.fence_ref, native_session_ref=decision.native_session_ref,
                    graph_ref=decision.graph_ref, generation=decision.generation + 1,
                    frontier=frontier, state=state, action=action, selected_target_refs=refs,
                    rationale="An invalid selection must never authorize a launch.",
                    inbox_checkpoint=checkpoint, idempotency_key="invalid-batch-selection",
                )
        assert owner.query_bundle_dispatch_decisions(decision.run_ref) == (decision,)

        with sqlite3.connect(runtime.data_root.database) as connection:
            connection.execute(
                "UPDATE ar_bundle_dispatch_decisions SET selected_target_refs_json=? WHERE decision_ref=?",
                (canonical_json([second, first]), decision.decision_ref),
            )
        with pytest.raises(OwnerConflict, match="bundle_dispatch_decision_invalid"):
            owner.query_bundle_dispatch_decisions(decision.run_ref)
        with pytest.raises(OwnerConflict, match="bundle_dispatch_decision_invalid"):
            owner.settle_bundle_dispatch_launches(decision.decision_ref, authorizations={})
        assert tuple(owner.query_target_launch_ack(ref) for ref in (first, second)) == (None, None)
    finally:
        runtime.close()
