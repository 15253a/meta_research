"""The v2 capture contract binds scopes without duplicating their parent content."""
import json
from dataclasses import replace

import pytest

from meta_research.formal_run_bindings import (
    SELECTION_MANIFEST_SCHEMA, implementation_binding, implementation_set_hash,
    manifest_selection_entries,
)
from meta_research.owners.common import OwnerConflict, canonical_hash, canonical_json
import meta_research.target_run_finalizer as module
from meta_research.target_run_finalizer import TargetRunFinalizer
from test_target_root_finalizer import _EvidenceReader, _root_finalizer_fixture, _current_bundle_runtime


def test_v2_fingerprint_ignores_ordinal_and_encoding_but_keeps_scope():
    first = {'role': 'implementation', 'declared_relative_path': 'implementation/a.py',
             'artifact_kind': 'file', 'tree_hash': 'a' * 64,
             'selection_protocol': SELECTION_MANIFEST_SCHEMA, 'ordinal': 1,
             'media_type': 'application/zip', 'content_hash': '1' * 64, 'path': 'a.py'}
    changed = {**first, 'ordinal': 999, 'media_type': 'application/x-directory', 'content_hash': '2' * 64}
    assert implementation_binding([first], ['implementation/a.py']) == implementation_binding([changed], ['implementation/a.py'])
    assert implementation_set_hash([first]) != implementation_set_hash([{**first, 'declared_relative_path': 'implementation/b.py'}])


def _finalizer(runtime, lifecycle, memory, evidence, **kwargs):
    return TargetRunFinalizer(lifecycle=lifecycle, memory=memory,
        workspace_resolver=runtime.target_run_authorities.agent_runtime,
        evidence_reader=_EvidenceReader(evidence), **kwargs)


def test_parent_and_file_scopes_share_one_capture_even_when_live_child_changes(tmp_path, monkeypatch):
    runtime, lifecycle, memory, _, handle, workspace, evidence = _root_finalizer_fixture(tmp_path)
    try:
        path = workspace / 'outputs/result.json'
        document = json.loads(path.read_text())
        document['formal_runs'][0]['implementation_paths'] = ['implementation']
        data = workspace / 'outputs/data'
        data.mkdir()
        (data / 'one.txt').write_text('one')
        (data / 'two.txt').write_text('two')
        document['formal_runs'][0]['artifact_paths'].extend(
            ['outputs/data', 'outputs/data/one.txt', 'outputs/data/two.txt'])
        document['formal_runs'].append({'run_key': 'file-method', 'implementation_paths': ['implementation/train.py'],
            'artifact_paths': [], 'checkpoint_paths': [], 'evaluations': []})
        document['environment_candidates'] = [{'artifact_path': 'implementation/train.py',
            'name': 'Training code', 'purpose': 'Reuse the exact adopted code'},
            {'artifact_path': 'outputs/data/one.txt', 'name': 'One resource', 'purpose': 'Reuse one produced file'}]
        path.write_text(canonical_json(document))
        original = module._freeze_artifact
        captured = []
        def freeze(fd, ordinal, role, relative_path, **kwargs):
            artifact = original(fd, ordinal, role, relative_path, **kwargs)
            captured.append(relative_path)
            if relative_path == 'implementation':
                (workspace / 'implementation/train.py').write_text('changed after parent freeze')
            return artifact
        monkeypatch.setattr(module, '_freeze_artifact', freeze)
        finalizer = _finalizer(runtime, lifecycle, memory, evidence,
                               graph_authority=runtime.owners.research_graph)
        result = finalizer.finalize(handle=handle, evidence=evidence)
        assert result.status == 'completed', result.rejection_feedback
        manifest = memory.query(result.manifest_ref)
        assert manifest.schema_ref == SELECTION_MANIFEST_SCHEMA
        assert captured.count('implementation') == 1
        assert 'implementation/train.py' not in captured
        entries = {entry['declared_relative_path']: entry for entry in manifest_selection_entries(manifest)}
        parent, child = entries['implementation'], entries['implementation/train.py']
        assert parent['binding'] == child['binding']
        assert child['path'] == 'train.py' and child['artifact_kind'] == 'file'
        binding = next(entry.binding for entry in manifest.entries if entry.role == 'implementation')
        view = runtime.owners.research_memory.read_selection_page(binding, 'train.py')
        assert 'changed after parent freeze' not in str(view)
        assert 'train' in str(view)
        assert captured.count('outputs/data') == 1
        assert 'outputs/data/one.txt' not in captured and 'outputs/data/two.txt' not in captured
        graph = runtime.owners.research_graph
        facts = graph.query_target_formal_results(handle.target_ref)
        primary = next(item for item in facts if item['run_key'] == 'primary')
        assert len([role for role in primary['run_artifacts'] if role['role'] == 'data_asset']) == 1
        scopes = primary['variant_run']['inputs']['produced_selections']
        assert {scope['artifact_path'] for scope in scopes} >= {
            'outputs/data', 'outputs/data/one.txt', 'outputs/data/two.txt'}
        quest_ref = runtime.target_run_authorities.agent_runtime.query_target_workspace_quest_ref(handle)
        handoff = graph.query_target_result_handoff(handle.target_ref, quest_ref=quest_ref)
        candidates = handoff['resource_candidates']['environment_candidates']
        resource = next(item for item in candidates if item['artifact_path'] == 'outputs/data/one.txt')
        assert resource['producers']
        assert {producer['subject_ref'] for producer in resource['producers']} == {primary['variant_run_ref']}
        assert finalizer.finalize(handle=handle, evidence=evidence) == result
    finally:
        runtime.close()


@pytest.mark.parametrize('bad', ['json', 'path', 'retained'])
def test_invalid_declaration_is_persisted_before_any_freeze_or_intake(tmp_path, monkeypatch, bad):
    runtime, lifecycle, memory, _, handle, workspace, evidence = _root_finalizer_fixture(tmp_path)
    try:
        path = workspace / 'outputs/result.json'
        original = path.read_text()
        if bad == 'json':
            path.write_text('{"metrics":{},"metrics":{}}')
        else:
            document = json.loads(original)
            if bad == 'path':
                document['formal_runs'][0]['implementation_paths'] = ['implementation/../outside']
            else:
                document['retained_artifacts'].append({
                    'relative_path': 'implementation/train.py', 'role': 'analysis'})
            path.write_text(canonical_json(document))
        finalizer = _finalizer(runtime, lifecycle, memory, evidence,
                               graph_authority=runtime.owners.research_graph)
        def forbidden(*args, **kwargs):
            pytest.fail('invalid declaration crossed freeze/intake/register')
        monkeypatch.setattr(finalizer, '_freeze', forbidden)
        monkeypatch.setattr(memory, 'accept', forbidden)
        monkeypatch.setattr(runtime.owners.research_graph, 'accept_target_commit_from_root_completion', forbidden)
        result = finalizer.finalize(handle=handle, evidence=evidence)
        assert result.status == 'revision_required'
        completion = lifecycle.query_completion(handle.target_ref)
        assert all(getattr(completion, name) is None for name in (
            'implementation_revision_ref', 'implementation_tree_hash', 'result_document_hash', 'artifact_snapshot_hash'))
        assert '$' in completion.candidate_rejection_feedback
        path.write_text(original)
        assert finalizer.finalize(handle=handle, evidence=evidence) == result
        assert lifecycle.query_completion(handle.target_ref) == completion
    finally:
        runtime.close()


def test_inline_snapshot_survives_restart_after_ar_receipt_before_rm(tmp_path, monkeypatch):
    runtime, lifecycle, memory, _, handle, workspace, evidence = _root_finalizer_fixture(tmp_path)
    finalizer = _finalizer(runtime, lifecycle, memory, evidence)
    def crash(**kwargs):
        raise RuntimeError('simulated process exit after AR acceptance')
    with monkeypatch.context() as fault:
        fault.setattr(memory, 'accept', crash)
        with pytest.raises(RuntimeError, match='simulated process exit'):
            finalizer.finalize(handle=handle, evidence=evidence)
    completion = lifecycle.query_completion(handle.target_ref)
    assert completion is not None
    (workspace / 'implementation/train.py').write_text('different live code')
    (workspace / 'outputs/result.json').write_text('invalid later live result')
    runtime.close()
    reopened = _current_bundle_runtime(tmp_path / 'target-root-finalizer')
    try:
        finalizer = _finalizer(reopened, reopened.target_root_lifecycle,
                               reopened.target_run_finalizer._memory, evidence)
        result = finalizer.finalize(handle=handle, evidence=evidence)
        assert result.status == 'rm_accepted'
        assert result.completion_ref == completion.completion_ref
        manifest = finalizer._memory.query(result.manifest_ref)
        entry = next(entry for entry in manifest.entries if entry.role == 'implementation')
        page = reopened.owners.research_memory.read_selection_page(entry.binding, 'train.py')
        assert 'different live code' not in str(page)
        assert 'train' in str(page)
    finally:
        reopened.close()


def test_local_inputs_keep_child_and_whole_asset_scopes_in_run_contract():
    from meta_research.formal_run_bindings import resolve_local_inputs
    binding = {'asset_ref': 'parent', 'version_ref': 'version', 'content_hash': 'a' * 64,
               'manifest_hash': 'b' * 64, 'receipt': {'status': 'accepted', 'issuer': 'research_memory',
                   'kind': 'asset_accepted', 'receipt_ref': 'r', 'subject_ref': 'version', 'payload_hash': 'c' * 64}}
    entries = [{'declared_relative_path': 'outputs/data/' + child, 'role': 'data',
        'binding': binding, 'path': child, 'selection_protocol': SELECTION_MANIFEST_SCHEMA}
        for child in ('a', 'b')]
    entries.append({'declared_relative_path': 'outputs/data', 'role': 'data',
        'binding': binding, 'path': None, 'selection_protocol': SELECTION_MANIFEST_SCHEMA})
    consumer = {'run_key': 'use', 'local_inputs': [
        {'producer_run_key': 'produce', 'artifact_path': entry['declared_relative_path']}
        for entry in entries]}
    document = {'formal_runs': [
        {'run_key': 'produce', 'artifact_paths': [entry['declared_relative_path'] for entry in entries]}, consumer]}
    refs, selections = resolve_local_inputs(document, entries, consumer)
    assert len(set(refs)) == 3 and all(ref.startswith('content_selection_') for ref in refs[:2])
    assert refs[2] == binding['version_ref']
    assert [value['path'] for value in selections] == ['a', 'b', None]
    assert [value['selection_ref'] for value in selections[:2]] == refs[:2]
    assert selections[2]['selection_ref'].startswith('content_selection_')

