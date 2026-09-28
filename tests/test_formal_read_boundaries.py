"""Bundle source and Target completion context reads share short, fresh cuts."""
from unittest.mock import Mock

import pytest
from sqlalchemy import event, text

from meta_research.owners.common import OwnerConflict
from meta_research.target_run_finalizer import TargetRunFinalizer
from test_target_root_finalizer import _EvidenceReader, _root_finalizer_fixture


@pytest.fixture
def pending_root(tmp_path):
    runtime, lifecycle, memory, authority, handle, _, evidence = _root_finalizer_fixture(tmp_path)
    try:
        seeded = TargetRunFinalizer(
            lifecycle=lifecycle, memory=memory,
            workspace_resolver=runtime.target_run_authorities.agent_runtime,
            evidence_reader=_EvidenceReader(evidence),
            measurement_authority=runtime.owners.research_graph,
        ).finalize(handle=handle, evidence=evidence)
        completion = lifecycle.query_completion(handle.target_ref)
        manifest = memory.query(seeded.manifest_ref)
        assert completion is not None and manifest is not None
        yield runtime, authority, completion, manifest
    finally:
        runtime.close()


@pytest.mark.parametrize('kind', ['root_context', 'formal_content'])
def test_formal_reads_use_fresh_nested_cuts_and_keep_integrity(pending_root, monkeypatch, kind):
    runtime, authority, completion, manifest = pending_root
    owner, database = runtime.owners.research_graph, runtime._database
    values = dict(completion=completion, manifest=manifest, result_document=manifest.result_document)
    query = (lambda: owner._target_root_domain_context(**values)) if kind == 'root_context' else (
        lambda: owner.query_formal_plan_content_acceptance(authority.formal_plan_ref))
    expected = query()
    with database.read() as connection:
        row = connection.execute(text('SELECT plan_document_hash, object_path, content_ref FROM rm_plan_documents WHERE plan_document_hash=:hash'),
                                 {'hash': authority.plan_document_hash}).one()
    cuts, writes = [], []

    def observe(_conn, _cursor, statement, _parameters, _context, _many):
        verb = statement.lstrip().split(None, 1)[0].upper()
        if verb == 'SELECT':
            cut, cache = database._read_cut.get(), database._read_cache.get()
            assert cut is not None and cache is not None
            cuts.append((cut, cache))
        if verb in {'INSERT', 'UPDATE', 'DELETE', 'REPLACE'}:
            writes.append(statement)

    event.listen(database._engine, 'before_cursor_execute', observe)
    try:
        assert query() == expected
        first_cache = cuts[-1][1]
        assert query() == expected
        assert cuts[-1][1] is not first_cache
        with database.read_snapshot() as connection:
            offset = len(cuts)
            value = query()
            assert value == expected
            if kind == 'root_context':
                value[0].spec['mutation_probe'] = True
                assert query() == expected
            assert all(cut is connection and cache is database._read_cache.get() for cut, cache in cuts[offset:])
        assert not writes
    finally:
        event.remove(database._engine, 'before_cursor_execute', observe)
    assert database._read_cut.get() is None and database._read_cache.get() is None

    with database.write() as connection:
        connection.execute(text('UPDATE rm_plan_documents SET plan_document_hash=:hash WHERE content_ref=:ref'),
                           {'hash': '0' * 64, 'ref': row.content_ref})
    with pytest.raises(OwnerConflict):
        query()
    assert database._read_cut.get() is None and database._read_cache.get() is None
    with database.write() as connection:
        connection.execute(text('UPDATE rm_plan_documents SET plan_document_hash=:hash WHERE content_ref=:ref'),
                           {'hash': row.plan_document_hash, 'ref': row.content_ref})
    plan_file = runtime.owners.research_memory._object_store / row.object_path
    saved = plan_file.read_bytes()
    with database.read_snapshot():
        assert query() == expected
        try:
            plan_file.write_bytes(saved + b' ')
            with pytest.raises(OwnerConflict, match='plan_content_custody_unavailable'):
                query()
        finally:
            plan_file.write_bytes(saved)
        assert query() == expected
    verifier = owner._receipt_verifier
    original = verifier.verify_formal_plan_decision
    monkeypatch.setattr(verifier, 'verify_formal_plan_decision', Mock(side_effect=OwnerConflict('proof_unavailable')))
    with pytest.raises(OwnerConflict, match='proof_unavailable'):
        query()
    assert database._read_cut.get() is None and database._read_cache.get() is None
    monkeypatch.setattr(verifier, 'verify_formal_plan_decision', original)
    assert query() == expected


def test_root_acceptance_rechecks_changed_authority_before_writing(pending_root, monkeypatch):
    runtime, authority, completion, manifest = pending_root
    owner, database = runtime.owners.research_graph, runtime._database
    values = dict(completion=completion, manifest=manifest, result_document=manifest.result_document,
                  idempotency_key='fresh-context-root-acceptance')
    domain = owner._target_root_domain_context
    query_authority = owner.query_target_measurement_domain_authority
    caches = []
    calls = 0

    def inspect_authority(target_ref):
        assert database._read_cut.get() is not None
        cache = database._read_cache.get()
        assert cache is not None
        caches.append(cache)
        return query_authority(target_ref)

    def change_after_first_context(**kwargs):
        nonlocal calls
        assert database._read_cut.get() is None
        result = domain(**kwargs)
        assert database._read_cut.get() is None and database._read_cache.get() is None
        calls += 1
        if calls == 1:
            with database.write() as connection:
                connection.execute(text('UPDATE rg_target_measurement_domain_authorities SET authority_hash=:hash WHERE target_ref=:ref'),
                                   {'hash': '0' * 64, 'ref': authority.target_ref})
        return result

    monkeypatch.setattr(owner, 'query_target_measurement_domain_authority', inspect_authority)
    monkeypatch.setattr(owner, '_target_root_domain_context', change_after_first_context)
    with pytest.raises(OwnerConflict, match='target_measurement_domain_authority_integrity_invalid'):
        owner.accept_target_commit_from_root_completion(**values)
    assert len(caches) == 2 and caches[0] is not caches[1]
    assert database._read_cut.get() is None and database._read_cache.get() is None
    with database.write() as connection:
        assert connection.execute(text('SELECT COUNT(*) FROM rg_target_commits WHERE target_ref=:ref'),
                                  {'ref': authority.target_ref}).scalar_one() == 0
        connection.execute(text('UPDATE rg_target_measurement_domain_authorities SET authority_hash=:hash WHERE target_ref=:ref'),
                           {'hash': authority.authority_hash, 'ref': authority.target_ref})
    monkeypatch.setattr(owner, '_target_root_domain_context', domain)
    accepted = owner.accept_target_commit_from_root_completion(**values)
    assert accepted.target_ref == authority.target_ref
    assert database._read_cut.get() is None and database._read_cache.get() is None
