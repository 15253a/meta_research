"""Actual method choice and unmeasured work retain the normal truth boundary."""
import json

import pytest
from sqlalchemy import text

from meta_research.owners.common import OwnerConflict, canonical_json
from meta_research.target_commit_evidence import TargetCommitEvidenceCatalog
from test_formal_cross_baseline import _register_method
from test_open_evidence_eligibility import _accept_unmeasured, _quest_ref
from test_root_formal_entities import _accept
from test_target_root_finalizer import _root_finalizer_fixture


@pytest.mark.parametrize('evaluated', [False, True])
def test_primary_may_explicitly_select_an_existing_cross_baseline_variant(tmp_path, evaluated):
    runtime,lifecycle,memory,authority,handle,workspace,evidence = _root_finalizer_fixture(tmp_path)
    try:
        with runtime._database.write() as connection:
            baseline,variant = _register_method(connection,
                {'method_key':'alternative','method_version':'1','method_contract':{'purpose':'different actual method'}},
                {'method':'count'})
        path=workspace/'outputs/metrics.json'; doc=json.loads(path.read_text())
        doc['formal_runs']=[{'run_key':'selected','variant_ref':variant,
            'evaluations':[{'attempt_key':'actual','metrics':doc['metrics']}] if evaluated else []}]
        path.write_text(canonical_json(doc))
        accepted,_ = _accept(runtime,lifecycle,memory,handle,evidence)
        fact, = runtime.owners.research_graph.query_target_formal_results(handle.target_ref)
        assert fact['variant_run']['variant_ref']==variant
        graph=runtime.owners.research_graph;quest=_quest_ref(runtime)
        page=graph.query_baselines(quest_ref=quest,limit=1)
        assert page['next_offset'] is not None
        baselines=[*page['items']]
        while page['next_offset'] is not None:
            page=graph.query_baselines(quest_ref=quest,limit=1,offset=page['next_offset'])
            baselines.extend(page['items'])
        assert baseline in {item['baseline_ref'] for item in baselines}
        assert len(baselines)>=2
        selected=graph.query_baseline_variants(baseline,quest_ref=quest,variant_ref=variant)['items'][0]
        assert selected['recipe'] and any(run['variant_run_ref']==fact['variant_run_ref'] for run in selected['runs'])
        assert graph.query_formal_result_by_ref(fact['variant_run_ref'],quest_ref=quest)['variant_run']['variant_ref']==variant
        if evaluated:
            assert fact['evaluation_attempt']['evaluation_ref'] != authority.identities.evaluation_ref
        else:
            assert fact['evaluation_attempt'] is None
        assert _accept(runtime,lifecycle,memory,handle,evidence)[0]==accepted
    finally:
        runtime.close()


@pytest.mark.parametrize('corruption', ['receipt','empty_formal'])
def test_work_product_corruption_propagates_to_formal_reasoning_and_plan(tmp_path,corruption):
    runtime,graph,commit=_accept_unmeasured(tmp_path)
    try:
        quest=_quest_ref(runtime);catalog=TargetCommitEvidenceCatalog(graph,runtime.owners.research_memory)
        revision,entries=catalog.query_plan_evidence_catalog(quest_ref=quest,target_commit_refs=(commit.commit_ref,))
        kwargs=dict(quest_ref=quest,evidence_catalog=list(entries),expected_reference_revision=revision,
            evidence_reuse_set=[{'evidence_ref':entries[0]['evidence_ref'],'purpose':'Use observed work as a premise.'}])
        assert catalog.resolve_plan_evidence_reuse_leaves(**kwargs)
        with runtime._database.write() as connection:
            if corruption=='receipt':
                connection.execute(text("UPDATE rg_experiment_asset_roles SET receipt_hash=:hash WHERE role='log_asset'"),{'hash':'0'*64})
            else:
                connection.execute(text('DELETE FROM rg_target_root_formal_entities'))
        for read in (lambda:graph.query_target_formal_results(commit.target_ref),
                     lambda:graph.resolve_reasoning_historical_evidence_leaf(quest_ref=quest,ref=commit.commit_ref),
                     lambda:catalog.resolve_reasoning_target_evidence_leaves(quest_ref=quest,target_commit_refs=(commit.commit_ref,)),
                     lambda:catalog.resolve_plan_evidence_reuse_leaves(**kwargs)):
            with pytest.raises(OwnerConflict): read()
    finally:
        runtime.close()


def test_explicit_variant_from_another_quest_is_rejected(tmp_path):
    from test_research_datasets import _quest
    runtime,lifecycle,memory,authority,handle,workspace,evidence=_root_finalizer_fixture(tmp_path)
    try:
        current=_quest_ref(runtime)
        other=_quest(runtime,'foreign-formal-method')
        with runtime._database.write() as connection:
            baseline,variant=_register_method(connection,
                {'method_key':'foreign','method_version':'1','method_contract':{'purpose':'another quest method'}},
                {'method':'inspect'})
            connection.execute(text('UPDATE rg_experiment_baselines SET quest_ref=:quest WHERE baseline_ref=:ref'),
                {'quest':other.quest_ref if hasattr(other,'quest_ref') else other['quest_ref'], 'ref':baseline})
        graph=runtime.owners.research_graph
        assert graph.query_baseline(baseline,quest_ref=current) is None
        assert baseline not in {item['baseline_ref'] for item in graph.query_baselines(quest_ref=current)['items']}
        path=workspace/'outputs/metrics.json';doc=json.loads(path.read_text())
        doc['formal_runs']=[{'run_key':'foreign','variant_ref':variant,'evaluations':[]}]
        path.write_text(canonical_json(doc))
        with pytest.raises(OwnerConflict,match='variant_scope_invalid'):
            _accept(runtime,lifecycle,memory,handle,evidence)
    finally:
        runtime.close()
