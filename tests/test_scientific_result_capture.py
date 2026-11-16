"""Result intake binds one parsed declaration and capture plan to exact bytes."""
import hashlib
import json
import os
from types import SimpleNamespace

import pytest

import meta_research.formal_entities as formal
import meta_research.target_run_finalizer as finalizer
from meta_research.owners.common import OwnerConflict, canonical_hash


def test_original_result_decoder_keeps_exact_scientific_integers():
    from pathlib import Path
    path = Path(__file__).parent / 'fixtures/scientific-result.original.json'
    if not path.exists():
        path = Path(__file__).parents[2] / 'seed-root-cause-20261116/result.original.json'
    raw = path.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == 'a2e3fbf3441f324c184d41622ec364727b65fa98864ca922b098daee72483b24'
    decoded = finalizer._decode_result_document_bytes(raw)
    original = json.loads(raw)
    assert decoded.as_dict() == original
    seeds = [item['bootstrap_seed'] for item in decoded.domain_fields['real_primary_intervals']]
    assert len(seeds) == 4 and all(type(value) is int for value in seeds)
    assert 14748774831839608929 in seeds


@pytest.mark.parametrize('metrics', [None, {}])
def test_formal_work_can_omit_summary_without_changing_document(metrics):
    document = {'formal_runs': [{'run_key': 'actual', 'evaluations': [
        {'attempt_key': 'assessment', 'metrics': {'seed': 2**100}}]}]}
    if metrics is not None:
        document['metrics'] = metrics
    raw = json.dumps(document).encode()
    result = finalizer._decode_result_document_bytes(raw)
    assert result.as_dict() == document
    assert result.content_hash == canonical_hash(document)


def test_capture_reuses_single_declaration_and_original_result_bytes(tmp_path, monkeypatch):
    (tmp_path / 'implementation').mkdir()
    (tmp_path / 'implementation/method.txt').write_text('actual method')
    (tmp_path / 'outputs').mkdir()
    original = {'metrics': {'seed': 2**100}, 'formal_runs': [{'run_key': 'actual', 'evaluations': []}]}
    raw = json.dumps(original, indent=2).encode()
    result_path = tmp_path / 'outputs/result.json'
    result_path.write_bytes(raw)
    evidence = SimpleNamespace(final_text='Actual completed work.', final_text_sha256='a' * 64)
    handle = SimpleNamespace(target_ref='target', target_run_ref='run')
    calls = []
    declared_work = formal._declared_work
    def counted(document):
        calls.append(document)
        return declared_work(document)
    monkeypatch.setattr(formal, '_declared_work', counted)
    pinned = finalizer._pin_workspace_root('workspace', tmp_path)
    try:
        candidate = finalizer._declared_target_completion_candidate(handle=handle, evidence=evidence,
            root_descriptor=pinned.descriptor)
        assert isinstance(candidate, finalizer.DeclaredCandidate)
        assert len(calls) == 1
        # A later writer replaces the live file. Freeze the original immutable
        # bytes associated with the plan, never the new file with the old plan.
        result_path.write_bytes(b'{"metrics":{"other":0}}')
        owner = object.__new__(finalizer.TargetRunFinalizer)
        with finalizer._completion_staging_directory(pinned) as staging:
            from pathlib import Path
            frozen = owner._freeze(handle=handle, handoff=candidate.handoff, resolved_workspace=pinned,
                final_text=evidence.final_text, capture_plan=candidate.capture_plan, staging_directory=Path(staging))
            artifact = next(item for item in frozen.artifacts if item.role == 'result')
            assert artifact.content == raw
            assert artifact.content_hash == hashlib.sha256(raw).hexdigest()
            assert frozen.result_document.as_dict() == original
            assert len(calls) == 1
    finally:
        os.close(pinned.descriptor)


@pytest.mark.parametrize('document', [
    {'formal_runs': []}, {'formal_runs': None}, {},
    {'formal_runs': [{'run_key': 'skipped', 'status': 'blocked'}]},
    {'metrics': None, 'formal_runs': [{'run_key': 'actual'}]},
])
def test_invalid_or_absent_formal_work_does_not_waive_summary(document):
    with pytest.raises(OwnerConflict):
        finalizer._decode_result_document_bytes(json.dumps(document).encode())


def test_small_document_canonical_hash_is_unchanged():
    document = {'metrics': {'value': 1.0, 'count': 1}, 'note': '观察'}
    assert finalizer._decode_result_document_bytes(json.dumps(document).encode()).content_hash == canonical_hash(document)


def test_prepared_result_reuses_exact_canonical_bytes_and_hash(monkeypatch):
    document = {'metrics': {'value': 2**100}, 'research': {'count': 1}}
    canonical = finalizer.canonical_json
    calls = []
    def counted(value):
        calls.append(value)
        return canonical(value)
    monkeypatch.setattr(finalizer, 'canonical_json', counted)
    decoded, encoded = finalizer._parse_result_document_bytes(json.dumps(document).encode())
    assert calls == [document]
    assert encoded == canonical(document).encode()
    assert decoded.content_hash == hashlib.sha256(encoded).hexdigest()


def test_relaxed_artifact_and_candidate_lists_preserve_last_product_through_owners(tmp_path):
    from test_target_root_finalizer import _EvidenceReader, _root_finalizer_fixture
    from meta_research.bundle_protocol import validate_closed_bundle_projection

    runtime, lifecycle, memory, authority, handle, workspace, evidence = _root_finalizer_fixture(tmp_path)
    try:
        path = workspace / 'outputs/result.json'
        document = json.loads(path.read_bytes())
        products = []
        implementations = []
        (workspace / 'outputs/data').mkdir()
        for index in range(107):
            product = f'outputs/data/product-{index:03}.txt'
            implementation = f'implementation/method-{index:03}.txt'
            (workspace / product).write_text(f'exact observation {index}', encoding='utf-8')
            (workspace / implementation).write_text(f'method component {index}', encoding='utf-8')
            products.append(product)
            implementations.append(implementation)
        document['formal_runs'][0]['artifact_paths'] = products
        document['formal_runs'][0]['implementation_paths'] = implementations
        document['retained_artifacts'].extend({'relative_path': product, 'role': 'data'} for product in products)
        for kind in ('dataset_candidates', 'environment_candidates'):
            document[kind] = [{'artifact_path': product, 'name': f'Observation {index}',
                'purpose': 'Preserve and reuse the exact observed research evidence.'}
                for index, product in enumerate(products)]
        raw = json.dumps(document).encode()
        assert len(raw) < 256 * 1024
        path.write_bytes(raw)
        owner = finalizer.TargetRunFinalizer(lifecycle=lifecycle, memory=memory,
            workspace_resolver=runtime.target_run_authorities.agent_runtime,
            evidence_reader=_EvidenceReader(evidence), measurement_authority=runtime.owners.research_graph,
            graph_authority=runtime.owners.research_graph)
        accepted = owner.finalize(handle=handle, evidence=evidence)
        assert accepted.status == 'completed', accepted
        manifest = memory.query(accepted.manifest_ref)
        entries = {entry.declared_relative_path: entry for entry in manifest.entries}
        assert set(products + implementations) <= entries.keys()
        last = entries[products[-1]]
        assert runtime.owners.research_memory.materialize_asset(last.binding.version_ref).content == b'exact observation 106'
        assert runtime.owners.research_memory.materialize_asset(entries['outputs/result.json'].binding.version_ref).content == raw
        facts = runtime.owners.research_graph.query_target_formal_results(handle.target_ref)
        assert len(facts) == 1
        assert len(facts[0]['run_artifacts']) >= 107
        transition = runtime.owners.research_graph.query_target_frontier_commit_transition(handle.target_ref)
        validate_closed_bundle_projection(transition.canonical_terminal, 'relaxed artifact lists')
    finally:
        runtime.close()


def test_capacity_and_independent_declaration_feedback():
    raw = b'{"metrics":{"observation":"' + b'x' * (300 * 1024) + b'"}}'
    with pytest.raises(finalizer.DeclaredCandidateIssue) as raised:
        finalizer._decode_result_document_bytes(raw)
    assert raised.value.code == 'target_root_result_document_too_large'
    assert str(len(raw)) in raised.value.feedback and '262144' in raised.value.feedback
    assert 'technical capacity' in raised.value.feedback
    deep = 0
    for _ in range(65):
        deep = [deep]
    with pytest.raises(finalizer.DeclaredCandidateIssue) as raised:
        finalizer._decode_result_document_value({'metrics': {'observation': deep}})
    assert raised.value.code == 'target_root_result_document_too_large'
    assert '64' in raised.value.feedback and 'scientific_depth' in raised.value.feedback
    with pytest.raises(finalizer.DeclaredCandidateIssue) as raised:
        finalizer._decode_result_document_value({'metrics': {'observed': 1},
            'schema_ref': 42, 'result_disposition': [],
            'dataset_candidates': [{'artifact_path': '../outside', 'name': '', 'purpose': 42}]})
    feedback = raised.value.feedback
    for field in ('schema_ref', 'result_disposition', 'artifact_path', 'name', 'purpose'):
        assert field in feedback
    assert len(feedback) <= 4096


def test_result_capacity_checked_before_reading_oversize_file(tmp_path, monkeypatch):
    path = tmp_path / 'large.json'
    path.write_bytes(b'x' * (300 * 1024))
    descriptor = os.open(path, os.O_RDONLY)
    try:
        monkeypatch.setattr(finalizer.os, 'read', lambda *_args: pytest.fail('oversize result was read'))
        with pytest.raises(finalizer.DeclaredCandidateIssue, match='too_large'):
            finalizer._read_stable_regular_file(descriptor, os.fstat(descriptor), result_document=True)
    finally:
        os.close(descriptor)


@pytest.mark.parametrize('failure', ['source_changed', 'storage_service_error'])
def test_file_failure_keeps_source_or_service_category(tmp_path, monkeypatch, failure):
    path = tmp_path / 'result.json'
    path.write_bytes(b'{"metrics":{"value":1}}')
    descriptor = os.open(path, os.O_RDONLY)
    read = os.read
    def unstable_read(fd, count):
        if failure == 'storage_service_error':
            raise OSError('isolated storage failure')
        chunk = read(fd, count)
        path.write_bytes(b'changed source')
        return chunk
    try:
        before = os.fstat(descriptor)
        monkeypatch.setattr(finalizer.os, 'read', unstable_read)
        with pytest.raises(OwnerConflict) as raised:
            finalizer._read_stable_regular_file(descriptor, before,
                result_document=True, relative_path='outputs/result.json')
        assert raised.value.details['category'] == failure
        assert raised.value.details['path'] == 'outputs/result.json'
        assert 'correction' in raised.value.details
    finally:
        os.close(descriptor)
