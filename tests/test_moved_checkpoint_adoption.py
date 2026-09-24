"""Stable checkpoint identity survives duplicate ordinals and later adoption."""
from dataclasses import replace
import json

import pytest
from sqlalchemy import text

from meta_research.owners.common import OwnerConflict, canonical_json
from meta_research.target_run_runtime_contract import TargetCompletionArtifact
import test_public_bundle_stage as bundle_fixtures
import test_target_root_finalizer as fixtures
from test_root_checkpoint_assignment import _with_checkpoint
from test_run_only_registration import _finalizer


def _next_target(runtime, authority, handle, tmp_path, monkeypatch):
    graph_owner = runtime.owners.research_graph
    with runtime._database.read() as connection:
        request_ref = connection.execute(text('SELECT request_ref FROM rg_target_graphs WHERE graph_ref=:ref'),
                                         {'ref': authority.graph_ref}).scalar_one()
    graph = graph_owner.query_target_graph(request_ref)
    second = next(target for target in graph.targets if target.target_ref != handle.target_ref)
    run = runtime.owners.agent_runtime.query_bundle_stage_run(request_ref)
    previous = runtime.owners.agent_runtime.query_bundle_dispatch_decisions(run.run_ref)[-1]
    checkpoint = runtime.bundle_stage._drain_bundle_inbox(run)
    decision = runtime.owners.agent_runtime.record_bundle_dispatch_decision(
        run_ref=run.run_ref, attempt_ref=run.attempt_ref, fence_ref=run.fence_ref,
        native_session_ref=run.native_session_ref, graph_ref=graph.graph_ref,
        generation=previous.generation+1,
        frontier=tuple(item for item in previous.frontier if item['target_ref'] == second.target_ref),
        state=runtime.bundle_stage._dispatch_state(graph, (), run=run), action='dispatch',
        selected_target_ref=second.target_ref, rationale='Assess a corrected state attribution.',
        inbox_checkpoint=checkpoint, idempotency_key='moved-state-dispatch')
    graph_owner.accept_target_candidate_projection(target_ref=second.target_ref, idempotency_key='second-projection')
    request = graph_owner.query_target_launch_request(second.target_ref)
    with monkeypatch.context() as patch:
        patch.setattr(fixtures, '_current_bundle_runtime', lambda path: runtime)
        patch.setattr(fixtures, '_ready_launch', lambda value: (graph,second,run,decision,request))
        for owner, method in ((graph_owner,'accept_target_candidate_projection'),
                (runtime.owners.agent_runtime,'admit_target_launch'),
                (runtime.target_run_authorities.research_graph,'accept_execution_input_binding'),
                (fixtures.SQLiteTargetRootLifecycleAuthority,'activate')):
            original = getattr(owner, method)
            def invoke(*args, _original=original, **kwargs):
                if 'idempotency_key' in kwargs: kwargs['idempotency_key'] += ':second'
                return _original(*args, **kwargs)
            patch.setattr(owner,method,invoke)
        return fixtures._root_finalizer_fixture(tmp_path/'second')


@pytest.mark.parametrize('new_owner', ['B', 'A'])
def test_moved_state_old_evaluation_replays_but_new_evaluation_uses_current_owner(tmp_path, monkeypatch, new_owner):
    monkeypatch.setattr(fixtures._CurrentBindingBundleSkill, '_target_plan',
                        bundle_fixtures._ParallelTwoTargetBundleSkill._target_plan)
    monkeypatch.setattr(fixtures._CurrentBindingBundleSkill, '_second_dependencies',lambda self:(),raising=False)
    runtime,lifecycle,memory,authority,handle,workspace,evidence = fixtures._root_finalizer_fixture(tmp_path/'first')
    try:
        evidence = _with_checkpoint(workspace,evidence)
        (workspace/'outputs/b.ckpt').write_bytes(b'B-state')
        evidence = replace(evidence,handoff=replace(evidence.handoff,artifacts=(*evidence.handoff.artifacts,
            TargetCompletionArtifact(role='checkpoint',relative_path='outputs/b.ckpt'))))
        path=workspace/'outputs/metrics.json'; doc=json.loads(path.read_text())
        doc['formal_runs']=[
            {'run_key':'A','checkpoint_paths':['outputs/final.ckpt'],
             'evaluations':[{'attempt_key':'original','metrics':doc['metrics']}]},
            {'run_key':'B','checkpoint_paths':['outputs/b.ckpt'],'evaluations':[]},
            {'run_key':'C','checkpoint_paths':[],'evaluations':[]}]
        path.write_text(canonical_json(doc))
        accepted=_finalizer(runtime,lifecycle,memory,evidence).finalize(handle=handle,evidence=evidence)
        assert accepted.status=='completed'
        graph=runtime.owners.research_graph
        original={row['run_key']:row for row in graph.query_target_formal_results(handle.target_ref)}
        with runtime._database.read() as connection:
            roles={row['subject_ref']:dict(row) for row in connection.execute(text(
                "SELECT * FROM rg_experiment_asset_roles WHERE role='checkpoint_artifact'")).mappings()}
        a,b,c=[original[key]['variant_run_ref'] for key in ('A','B','C')]
        assert roles[a]['ordinal']==roles[b]['ordinal']==0
        role=roles[a]
        for index,destination in enumerate((b,c,b)):
            args=dict(role_ref=role['role_ref'],to_subject_kind='variant_run',to_subject_ref=destination,
                reason='Correct the actual owner of retained state.',idempotency_key=f'move-{index}')
            adjustment=graph.adjust_experiment_artifact_role(**args)
            assert graph.adjust_experiment_artifact_role(**args)==adjustment
        # Original acceptance and old assessment remain verifiable after multiple moves.
        assert _finalizer(runtime,lifecycle,memory,evidence).finalize(handle=handle,evidence=evidence)==accepted
        assert graph.query_target_formal_results(handle.target_ref)[0]['metric_result_ref']==original['A']['metric_result_ref']
        with runtime._database.read() as connection:
            moved=connection.execute(text('SELECT * FROM rg_experiment_asset_roles WHERE role_ref=:ref'),{'ref':role['role_ref']}).mappings().one()
            assert moved['subject_ref']==b and moved['ordinal']==0
            assert moved['receipt_hash']==role['receipt_hash']
            assert connection.exec_driver_sql('PRAGMA integrity_check').scalar_one()=='ok'
            assert connection.exec_driver_sql('PRAGMA foreign_key_check').all()==[]
        _,life2,mem2,_,handle2,space2,evidence2=_next_target(runtime,authority,handle,tmp_path,monkeypatch)
        evidence2=replace(evidence2,operation_ref='second-terminal',evidence_ref='second-evidence',
            handoff=replace(evidence2.handoff,artifacts=tuple(a for a in evidence2.handoff.artifacts if a.role!='checkpoint')))
        path2=space2/'outputs/metrics.json'; doc2=json.loads(path2.read_text()); old=original['A']; selected=original[new_owner]
        doc2['formal_runs']=[
            {'run_key':'old-A-assessment','variant_ref':old['variant_run']['variant_ref'],'variant_run_ref':a,
             'checkpoint_paths':['outputs/final.ckpt'], 'evaluations':[{'attempt_key':'old',
                'evaluation_ref':old['evaluation_attempt']['evaluation_ref'],
                'evaluation_attempt_ref':old['evaluation_attempt_ref'],'metric_result_ref':old['metric_result_ref'],
                'metrics':old['metric_result']['metrics']}]},
            {'run_key':'new-assessment','variant_ref':selected['variant_run']['variant_ref'],
             'variant_run_ref':selected['variant_run_ref'],'checkpoint_role_refs':([role['role_ref'],roles[b]['role_ref']] if new_owner=='B' else [role['role_ref']]),
             'evaluations':[{'attempt_key':'new','metrics':doc2['metrics'],'checkpoint_role_refs':[role['role_ref']]}]}]
        path2.write_text(canonical_json(doc2))
        if new_owner=='A':
            with pytest.raises(OwnerConflict,match='checkpoint_reference_not_bound'):
                _finalizer(runtime,life2,mem2,evidence2).finalize(handle=handle2,evidence=evidence2)
        else:
            finalizer=_finalizer(runtime,life2,mem2,evidence2)
            result=finalizer.finalize(handle=handle2,evidence=evidence2)
            assert result.status=='completed'
            facts=graph.query_target_formal_results(handle2.target_ref)
            assert facts[0]['evaluation_attempt_ref']==old['evaluation_attempt_ref']
            assert facts[1]['evaluation_attempt']['checkpoint_role_refs']==[role['role_ref']]
            assert finalizer.finalize(handle=handle2,evidence=evidence2)==result
    finally:
        runtime.close()
