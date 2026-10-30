from dataclasses import replace
from types import SimpleNamespace

import pytest

from meta_research.owners.common import OwnerConflict
from meta_research.bundle_protocol import ExperimentBrief
from meta_research.target_run_runtime import TargetRunRuntime
from test_target_input_reference_bridge import _authority
from test_target_run_owner import _records


def test_only_selected_evidence_source_provides_companion_artifacts():
    owner = _authority(('evidence_1',), plan=True)
    assert owner.selected_evidence_target_commits('target') == {'commit_1': ('evidence_1',)}


def test_unselected_foreign_evidence_cannot_supply_companions():
    owner = _authority(('evidence_not_in_this_plan',), plan=True)
    with pytest.raises(OwnerConflict, match='target_input_evidence_not_selected'):
        owner.selected_evidence_target_commits('target')


def test_evidence_source_commit_tamper_cannot_supply_foreign_artifacts():
    owner = _authority(('evidence_1',), plan=True)
    original = owner._domain_reader.resolve_plan_evidence_reuse_leaves
    leaves = list(original())
    leaves[0] = SimpleNamespace(**{**vars(leaves[0]), 'target_commit_ref':'commit_foreign'})
    owner._domain_reader.resolve_plan_evidence_reuse_leaves = lambda **kw: leaves
    with pytest.raises(OwnerConflict, match='target_input_evidence_catalog_invalid'):
        owner.selected_evidence_target_commits('target')


def test_no_selected_evidence_has_no_companion_expansion():
    owner = _authority(())
    assert owner.selected_evidence_target_commits('target') == {}


def test_target_prompt_has_complete_selected_science_without_unrelated_briefs():
    candidate, plan, handle, _preflight, request = _records()
    unrelated = ExperimentBrief('unselected-experiment', 'UNRELATED_BRIEF_SENTINEL', (), ('unit-unrelated',))
    plan = replace(plan, briefs=plan.briefs + (unrelated,))
    prompt = TargetRunRuntime._root_prompt(
        execution_contract={'measurement_contract': {'protocol_version': {'required_metric_keys':['f1']}}},
        handle=handle, candidate=candidate, formal_plan=plan,
        launch=SimpleNamespace(graph_ref="graph:target", stage_request_ref="stage-request:target", request=SimpleNamespace(target_spec_binding=request.target_spec_binding)),
        frozen_input_manifest_path='/isolated/frozen/manifest.json',
        research_context={'cycle_ref': 'cycle:target', 'graph_ref': 'graph:target', 'stage_request_ref': 'stage-request:target', 'target_ref': handle.target_ref, 'cycle_position': {'cycle_ref': 'cycle:target', 'round': 1,
            'cycle_focus': 'Audit patient split', 'focus_source': {'request_ref': 'fixture-start'}},
            'question':{'unknown_statement':'Is the patient split leakage-free?'},
            'obligations':[{'statement':'Audit actual patient overlap'}],
            'experiment_briefs':[{'goal':'Get independent split evidence'}],
            'plan_notes':'diagnostic priority', 'bundle_notes':'中文'*6000})
    assert 'patient split leakage-free' in prompt
    assert 'Audit actual patient overlap' in prompt
    assert 'Get independent split evidence' in prompt
    assert 'diagnostic priority' in prompt
    assert 'UNRELATED_BRIEF_SENTINEL' not in prompt
    assert '中文'*6000 in prompt
    assert 'exact_context_path' not in prompt
    assert prompt.startswith('cycle_position=')
    assert '"execution_input_binding_receipt"' not in prompt
    assert 'result_schema 是初始表达指导' in prompt
    assert 'Protocol 指标集合' in prompt
