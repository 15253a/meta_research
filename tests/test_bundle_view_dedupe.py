from collections import Counter
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from meta_research import bundle_batch_access as access
from meta_research import bundle_batch_view as views
from meta_research.owners.common import canonical_hash, canonical_json
from test_gemini_batch_delta import _rich_multi_cell_fixture, _source_readers


def test_source_descriptors_reuse_each_record_digest(tmp_path, monkeypatch):
    request, _, _, _, _ = _rich_multi_cell_fixture(tmp_path)
    calls = Counter()

    def digest(value):
        calls[id(value)] += 1
        return canonical_hash(value)

    documents = {item['target_ref']: item for item in request.current_targets}
    documents.update({item['commit_ref']: item for item in request.target_commits})
    documents[request.graph_ref] = request.initial_target_plan['completion_contract']
    monkeypatch.setattr(access, 'canonical_hash', digest)
    monkeypatch.setattr(access, 'read_stage_context', lambda *args: {
        'context_pack_hash': request.context_pack_hash,
        'content_hash': canonical_hash(documents[args[-1]['source_ref']]),
    })
    result = access.verified_batch_source_readers(request,
        advancement_engine=SimpleNamespace(query_bundle_stage_request=lambda _: SimpleNamespace(
            request_ref=request.stage_request_ref)),
        agent_runtime=SimpleNamespace(query_bundle_stage_run=lambda _: SimpleNamespace(
            native_session_ref=request.native_session_ref), query_bundle_target_proposals=lambda _: ()),
        research_graph=None, research_memory=None)
    assert result['target_commits']['commit:2']['arguments'] == {
        'context_pack_ref': request.context_pack_ref, 'source': 'bundle_target_commit',
        'source_ref': 'commit:2', 'version_ref': canonical_hash(documents['commit:2']),
        'path': [], 'offset': 0, 'limit': 16384,
    }
    assert result['completion_contract']['content_hash'] == canonical_hash(documents[request.graph_ref])
    assert {ref: calls[id(document)] for ref, document in documents.items()} == {
        ref: 1 for ref in documents}


def test_delta_reuses_record_digests_and_preserves_results(tmp_path, monkeypatch):
    request, prompt, plan, readers, baseline = _rich_multi_cell_fixture(tmp_path)
    calls = Counter()

    def digest(value):
        calls[canonical_json(value)] += 1
        return canonical_hash(value)

    monkeypatch.setattr(views, 'canonical_hash', digest)
    result = views.render_delta_batch_reading_context(request, prompt,
        verified_plan_reader=plan, verified_source_readers=readers,
        verified_delivery_baseline=baseline)
    assert result['current_targets'] == [request.current_targets[1]]
    assert result['target_commits'] == [request.target_commits[1]]
    indexes = result['responsibility_index']
    assert [entry['accepted_commit_refs'] for entry in indexes['current_targets']] == [['commit:1'], ['commit:2']]
    assert 'body_reason' not in indexes['target_commits'][0]
    assert indexes['target_commits'][1]['body_reason'] == 'new_or_changed_or_reader_unavailable'
    assert max(calls[canonical_json(item)] for item in (*request.current_targets, *request.target_commits)) <= 3


def test_delta_does_not_copy_discarded_plan_and_keeps_output_isolated(tmp_path):
    request, prompt, plan, readers, baseline = _rich_multi_cell_fixture(tmp_path)
    copied = []

    class PlanDetails(dict):
        def __deepcopy__(self, memo):
            copied.append(True)
            return deepcopy(dict(self), memo)

    document = {**request.plan_document, 'historical_detail': PlanDetails(prose='previous work')}
    request = replace(request, plan_document=document)
    prompt['formal_plan'] = document
    prompt['unknown_field'] = {'values': ['keep']}
    plan['plan_hash'] = canonical_hash(document)
    original = canonical_json(prompt)
    result = views.render_delta_batch_reading_context(request, prompt,
        verified_plan_reader=plan, verified_source_readers=readers,
        verified_delivery_baseline=baseline)
    assert result['target_commits'][0]['valid_results'] == 600
    assert result['unknown_field'] == {'values': ['keep']}
    assert copied == []
    result['unknown_field']['values'].append('change')
    result['target_commits'][0]['valid_results'] = 560
    result['responsibility_index']['current_targets'][0]['accepted_commit_refs'].append('change')
    assert canonical_json(prompt) == original
    assert request.target_commits[0]['commit_ref'] == 'commit:1'


@pytest.mark.parametrize('baseline_present', [False, True])
def test_duplicate_current_refs_preserve_each_body_reason(tmp_path, baseline_present):
    request, prompt, plan, _, baseline = _rich_multi_cell_fixture(tmp_path)
    revision = {**request.target_commits[0], 'late_revision': 'changed'}
    request = replace(request, target_commits=(request.target_commits[0], revision, request.target_commits[1]))
    prompt['target_commits'] = list(request.target_commits)
    result = views.render_delta_batch_reading_context(request, prompt,
        verified_plan_reader=plan, verified_source_readers=_source_readers(request),
        verified_delivery_baseline=baseline if baseline_present else None)
    assert result['target_commits'] == list(request.target_commits)
    expected = 'new_or_changed_or_reader_unavailable' if baseline_present else 'no_verified_delivery_basis'
    assert [item['body_reason'] for item in result['responsibility_index']['target_commits']] == [expected] * 3
