"""A partial completion must retain its own association to reused real work."""
import json
import pytest
from dataclasses import replace

from sqlalchemy import text

from meta_research.owners.common import canonical_json
import test_public_bundle_stage as bundle_fixtures
import test_target_root_finalizer as root_fixtures
from test_run_only_registration import _declare, _finalizer


@pytest.mark.parametrize('evaluate', [False, True])
def test_unassessed_cross_target_reuse_is_queryable_without_duplicating_execution(tmp_path, monkeypatch, evaluate):
    monkeypatch.setattr(root_fixtures._CurrentBindingBundleSkill, '_target_plan',
                        bundle_fixtures._ParallelTwoTargetBundleSkill._target_plan)
    monkeypatch.setattr(root_fixtures._CurrentBindingBundleSkill, '_second_dependencies', lambda self: (), raising=False)
    runtime, lifecycle, memory, authority, handle, workspace, evidence = root_fixtures._root_finalizer_fixture(tmp_path / 'first')
    try:
        _declare(workspace, evidence)
        pending = _finalizer(runtime, lifecycle, memory, evidence).finalize(handle=handle, evidence=evidence)
        graph_owner = runtime.owners.research_graph
        original = graph_owner.query_target_formal_results(handle.target_ref)
        with runtime._database.read() as connection:
            request_ref = connection.execute(text('SELECT request_ref FROM rg_target_graphs WHERE graph_ref=:ref'),
                                             {'ref': authority.graph_ref}).scalar_one()
        graph = graph_owner.query_target_graph(request_ref)
        second = next(target for target in graph.targets if target.target_ref != handle.target_ref)
        run = runtime.owners.agent_runtime.query_bundle_stage_run(request_ref)
        decisions = runtime.owners.agent_runtime.query_bundle_dispatch_decisions(run.run_ref)
        previous = decisions[-1]
        checkpoint = runtime.bundle_stage._drain_bundle_inbox(run)
        decision = runtime.owners.agent_runtime.record_bundle_dispatch_decision(
            run_ref=run.run_ref, attempt_ref=run.attempt_ref, fence_ref=run.fence_ref,
            native_session_ref=run.native_session_ref, graph_ref=graph.graph_ref,
            generation=previous.generation + 1,
            frontier=tuple(item for item in previous.frontier if item['target_ref'] == second.target_ref),
            state=runtime.bundle_stage._dispatch_state(graph, (), run=run), action='dispatch',
            selected_target_ref=second.target_ref, rationale='Assess an existing execution in a second independent Target.',
            inbox_checkpoint=checkpoint, idempotency_key='cross-target-reuse-dispatch')
        graph_owner.accept_target_candidate_projection(target_ref=second.target_ref,
            idempotency_key='accept-target-root-candidate-projection:second')
        request = graph_owner.query_target_launch_request(second.target_ref)
        # Reuse the established fixture's normal Owner admission sequence;
        # only its test-specific command keys need a second-Target namespace.
        with monkeypatch.context() as second_fixture:
            second_fixture.setattr(root_fixtures, '_current_bundle_runtime', lambda path: runtime)
            second_fixture.setattr(root_fixtures, '_ready_launch', lambda value: (graph, second, run, decision, request))
            def suffix(owner, method):
                original_method = getattr(owner, method)
                def invoke(*args, **kwargs):
                    if 'idempotency_key' in kwargs:
                        kwargs['idempotency_key'] += ':second'
                    return original_method(*args, **kwargs)
                second_fixture.setattr(owner, method, invoke)
            suffix(graph_owner, 'accept_target_candidate_projection')
            suffix(runtime.owners.agent_runtime, 'admit_target_launch')
            suffix(runtime.target_run_authorities.research_graph, 'accept_execution_input_binding')
            suffix(root_fixtures.SQLiteTargetRootLifecycleAuthority, 'activate')
            _, second_lifecycle, second_memory, _, second_handle, second_workspace, second_evidence = root_fixtures._root_finalizer_fixture(tmp_path / 'second')
        second_evidence = replace(second_evidence, operation_ref='second-target-final-turn',
                                  evidence_ref='second-target-final-evidence')
        result_path = second_workspace / 'outputs/metrics.json'
        document = json.loads(result_path.read_text())
        old = original[0]
        document['formal_runs'] = [{
            'run_key': 'reused-only', 'variant_run_ref': old['variant_run_ref'],
            'input_refs': old['variant_run']['inputs']['input_refs'],
            'checkpoint_paths': [], 'evaluations': [{'attempt_key': 'pending', 'status': 'blocked'}],
        }]
        if evaluate:
            document['formal_runs'][0]['evaluations'] = [{'attempt_key': 'later-assessment', 'metrics': document['metrics']}]
        else:
            document['metrics'] = {}
        result_path.write_text(canonical_json(document))
        finalizer = _finalizer(runtime, second_lifecycle, second_memory, second_evidence)
        reused = finalizer.finalize(handle=second_handle, evidence=second_evidence)
        assert reused.status == 'completed'
        assert reused.manifest_ref != pending.manifest_ref
        facts = graph_owner.query_target_formal_results(second_handle.target_ref)
        assert len(facts) == 1 and facts[0]['variant_run_ref'] == old['variant_run_ref']
        assert facts[0]['manifest_ref'] == reused.manifest_ref
        assert facts[0]['variant_run']['inputs']['target_ref'] == handle.target_ref
        assert (facts[0]['evaluation_attempt'] is not None) == evaluate
        assert facts[0]['target_commit_ref'] == reused.target_commit_ref
        assert finalizer.finalize(handle=second_handle, evidence=second_evidence) == reused
        assert graph_owner.query_target_formal_results(second_handle.target_ref) == facts
        with runtime._database.read() as connection:
            assert connection.execute(text('SELECT count(*) FROM rg_variant_runs')).scalar_one() == 2
            assert connection.execute(text('SELECT count(*) FROM rg_target_root_unassessed_runs')).scalar_one() == 0
            assert connection.exec_driver_sql('SELECT count(*) FROM rg_target_commits').scalar_one() == 2
            for table in ('rg_evaluation_attempts', 'rg_metric_results'):
                assert connection.exec_driver_sql('SELECT count(*) FROM ' + table).scalar_one() == int(evaluate)
            assert connection.exec_driver_sql('PRAGMA foreign_key_check').fetchall() == []
    finally:
        runtime.close()
