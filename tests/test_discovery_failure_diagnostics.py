"""Real scoped discovery failures must identify the caller's correctable fields."""
import json
from types import SimpleNamespace

import pytest

from meta_research.owners.common import OwnerConflict
from test_public_plan_stage import _confirm_direct_quest
from test_root_workspace import _make, _channel, _call


@pytest.fixture
def admitted(tmp_path):
    runtime = _make(tmp_path)
    try:
        _confirm_direct_quest(runtime)
        started = runtime.idea_stage.start('discovery-diagnostics')
        run = runtime.owners.agent_runtime.query_idea_stage_run(started['stage_run_request']['request_ref'])
        channel = _channel(runtime, SimpleNamespace(run_ref=run.run_ref, attempt_ref=run.attempt_ref,
            root_session_ref=run.root_session_ref, fence_ref=run.fence_ref,
            capability_binding_hash=run.runtime_binding_hash, root_kind='idea', phase='primary'))
        yield runtime, channel
    finally:
        runtime.close()


def failure(result, code):
    assert result['isError']
    assert result['structuredContent']['code'] == code
    assert json.loads(result['content'][0]['text']) == result['structuredContent']
    details = result['structuredContent']['details']
    assert details['message']
    return details


@pytest.mark.parametrize('operation,arguments,code,field', [
    ('research_graph.datasets.page', {'dataset_ref': 'x', 'question_ref': 'y'}, 'dataset_page_filter_invalid', 'dataset_ref'),
    ('research_graph.datasets.page', {'direction': 'sources'}, 'dataset_page_filter_invalid', 'dataset_version_ref'),
    ('research_graph.environments.page', {'environment_ref': 'x', 'question_ref': 'y'}, 'environment_page_filter_invalid', 'environment_ref'),
    ('research_graph.environments.page', {'query': 'instrument', 'semantic_key': 'instrument'}, 'environment_page_filter_invalid', 'query'),
])
def test_filter_diagnostic_preserves_business_code_and_allows_same_grant_correction(admitted, operation, arguments, code, field):
    runtime, channel = admitted
    details = failure(_call(runtime, channel, operation, **arguments), code)
    assert field in details['message']
    assert not _call(runtime, channel, operation)['isError']


def test_dataset_read_requires_one_reference_and_preserves_owner_diagnosis(admitted, monkeypatch):
    runtime, channel = admitted
    details = failure(_call(runtime, channel, 'research_graph.datasets.read'), 'dataset_read_ref_invalid')
    assert 'exactly one' in details['message'] and 'dataset_ref' in details['message']
    registered = _call(runtime, channel, 'research_graph.datasets.register', effect_id='original-dataset',
        semantic_key='diagnostics', name='Retained observations', meaning='Actual measured observations')
    assert not registered['isError']
    ref = registered['structuredContent']['result']['dataset_ref']
    graph = runtime.owners.research_graph
    original = graph.query_dataset
    def unavailable(*args, **kwargs):
        raise OwnerConflict('dataset_store_unavailable', {'message': 'Dataset store is temporarily unavailable.',
            'correction': 'Restore the store; retain the exact dataset_ref.'})
    monkeypatch.setattr(graph, 'query_dataset', unavailable)
    details = failure(_call(runtime, channel, 'research_graph.datasets.read', dataset_ref=ref), 'dataset_store_unavailable')
    assert 'exact dataset_ref' in details['correction']
    monkeypatch.setattr(graph, 'query_dataset', original)
    # Registering the global identity does not create a version in this Quest.
    restored = _call(runtime, channel, 'research_graph.datasets.read', dataset_ref=ref)
    assert not restored['isError']
    assert restored['structuredContent']['status'] == 'not_found'
    retained = _call(runtime, channel, 'research_graph.datasets.register.reconcile', effect_id='original-dataset')
    assert retained['structuredContent']['result']['dataset_ref'] == ref


def test_baseline_deep_reference_names_its_required_relation(admitted):
    runtime, channel = admitted
    details = failure(_call(runtime, channel, 'research_graph.baselines.read',
        baseline_ref='unbound-baseline', variant_run_ref='unbound-run'), 'baseline_query_invalid')
    assert details['path'] == ['relation']
    restored = _call(runtime, channel, 'research_graph.baselines.read', baseline_ref='unbound-baseline')
    assert not restored['isError']


@pytest.mark.parametrize('operation', ['research_graph.questions.page', 'research_memory.literature.page',
    'research_graph.datasets.page', 'research_graph.environments.page', 'research_graph.baselines.page'])
def test_discovery_revision_diagnostics_lead_to_valid_continuation(admitted, operation):
    runtime, channel = admitted
    first = _call(runtime, channel, operation, limit=1)
    assert not first['isError']
    revision = first['structuredContent']['query_revision']
    missing = failure(_call(runtime, channel, operation, offset=1, limit=1), 'research_query_revision_required')
    assert missing['path'] == ['query_revision']
    assert 'query_revision' in missing['correction']
    changed = failure(_call(runtime, channel, operation, offset=1, limit=1, query_revision='0' * 64), 'research_query_changed')
    assert 'offset=0' in changed['correction']
    assert not _call(runtime, channel, operation, offset=1, limit=1, query_revision=revision)['isError']


def test_literature_fulltext_diagnostic_preserves_actual_missing_status(admitted):
    runtime, channel = admitted
    submitted = dict(effect_id='literature-diagnostic', record_ref='doi:10.1000/diagnostic',
        title='Abstract only', source='Supplied abstract', fulltext_status='obtained')
    details = failure(_call(runtime, channel, 'research_memory.literature.register', **submitted), 'literature_fulltext_binding_required')
    assert 'fulltext_status' in details['message'] and 'asset_bindings' in details['message']
    absent = _call(runtime, channel, 'research_memory.literature.register.reconcile', effect_id=submitted['effect_id'])
    assert absent['structuredContent']['result'] is None
    accepted = _call(runtime, channel, 'research_memory.literature.register', **{**submitted, 'fulltext_status': 'missing'})
    assert not accepted['isError']
    assert accepted['structuredContent']['result']['fulltext_status'] == 'missing'
