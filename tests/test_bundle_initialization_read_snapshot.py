"""Bundle initialization reuses strict proofs and releases them before writing."""
from contextlib import contextmanager
from unittest.mock import Mock

import pytest
from sqlalchemy import text

from meta_research.owners.common import OwnerConflict
from test_public_bundle_stage import _bundle_runtime, _prepare_bundle_request


def test_bundle_initialization_read_cut_preserves_proofs_and_write_boundary(tmp_path, monkeypatch):
    runtime = _bundle_runtime(tmp_path / 'bundle-init')
    try:
        _prepare_bundle_request(runtime)
        database, owner = runtime._database, runtime.owners.advancement_engine
        current = runtime.bundle_stage._discover_active_cycle()
        expected = owner.query_bundle_stage_request(current.cycle_ref)
        assert expected is not None
        original_write = database.write
        validating = False
        cuts, writes = [], []

        @contextmanager
        def observe_write(*args, **kwargs):
            nonlocal validating
            assert database._read_cut.get() is None and database._read_cache.get() is None
            validating = False
            writes.append(True)
            with original_write(*args, **kwargs) as connection:
                yield connection

        def inspect(name):
            original = getattr(owner, name)
            def check(*args, **kwargs):
                if validating:
                    cut, cache = database._read_cut.get(), database._read_cache.get()
                    assert cut is not None and cache is not None
                    cuts.append((name, cut, cache))
                return original(*args, **kwargs)
            return check

        for name in ('_verify_cycle_question', '_verify_plan_idea_set', '_verify_bundle_formal_plan'):
            monkeypatch.setattr(owner, name, inspect(name))
        monkeypatch.setattr(database, 'write', observe_write)

        def ensure(key):
            nonlocal validating
            validating = True
            try:
                return owner.ensure_bundle_stage_request(
                    cycle_ref=expected.cycle_ref, accepted_question=expected.accepted_question,
                    accepted_formal_plan=expected.accepted_formal_plan,
                    accepted_idea_set=expected.accepted_idea_set, context_pack=expected.context_pack,
                    idempotency_key=key)
            finally:
                validating = False

        assert ensure('test:bundle-init:proof-cut') == expected
        assert writes
        required = {'_verify_cycle_question', '_verify_bundle_formal_plan'}
        if expected.accepted_idea_set is not None:
            required.add('_verify_plan_idea_set')
        assert required.issubset({name for name, _, _ in cuts})
        assert all(cut is cuts[0][1] and cache is cuts[0][2] for _, cut, cache in cuts)
        assert database._read_cut.get() is None and database._read_cache.get() is None

        content_ref = expected.accepted_formal_plan.content_ref
        with database.write() as connection:
            row = connection.execute(text('SELECT plan_document_hash, object_path FROM rm_plan_documents WHERE content_ref=:ref'),
                                     {'ref': content_ref}).one()
            connection.execute(text('UPDATE rm_plan_documents SET plan_document_hash=:bad WHERE content_ref=:ref'),
                               {'bad': '0' * 64, 'ref': content_ref})
        writes.clear()
        try:
            with pytest.raises(OwnerConflict):
                ensure('test:bundle-init:tampered-row')
            assert not writes
            assert database._read_cut.get() is None and database._read_cache.get() is None
        finally:
            with database.write() as connection:
                connection.execute(text('UPDATE rm_plan_documents SET plan_document_hash=:good WHERE content_ref=:ref'),
                                   {'good': row.plan_document_hash, 'ref': content_ref})
        path = runtime.owners.research_memory._object_store / row.object_path
        saved = path.read_bytes()
        writes.clear()
        try:
            path.write_bytes(saved + b' ')
            with pytest.raises(OwnerConflict, match='plan_content_custody_unavailable'):
                ensure('test:bundle-init:tampered-original')
            assert not writes
            assert database._read_cut.get() is None and database._read_cache.get() is None
        finally:
            path.write_bytes(saved)
        assert ensure('test:bundle-init:restored') == expected
    finally:
        runtime.close()


def test_stage_request_result_read_cut_covers_strict_conversion(tmp_path, monkeypatch):
    runtime = _bundle_runtime(tmp_path / 'request-result')
    try:
        _prepare_bundle_request(runtime)
        database, owner = runtime._database, runtime.owners.advancement_engine
        current = runtime.bundle_stage._discover_active_cycle()
        expected = owner.query_bundle_stage_request(current.cycle_ref)
        original = owner._stage_request_from_row
        cuts = []

        def inspect(row):
            cut, cache = database._read_cut.get(), database._read_cache.get()
            assert cut is not None and cache is not None
            cuts.append((cut, cache))
            return original(row)

        with monkeypatch.context() as patch:
            patch.setattr(owner, '_stage_request_from_row', inspect)
            patch.setattr(database, 'write', Mock(side_effect=AssertionError('result read must not write')))
            assert owner._query_stage_request_ref(expected.request_ref) == expected
            first_cache = cuts[-1][1]
            assert owner._query_stage_request_ref(expected.request_ref) == expected
            assert cuts[-1][1] is not first_cache
            with database.read_snapshot() as connection:
                assert owner._query_stage_request_ref(expected.request_ref) == expected
                assert cuts[-1][0] is connection
            with pytest.raises(OwnerConflict, match='stage_command_result_missing'):
                owner._query_stage_request_ref('stage-request-missing')
            assert database._read_cut.get() is None and database._read_cache.get() is None
            patch.setattr(owner, '_stage_request_from_row', Mock(side_effect=OwnerConflict('proof_unavailable')))
            with pytest.raises(OwnerConflict, match='proof_unavailable'):
                owner._query_stage_request_ref(expected.request_ref)
            assert database._read_cut.get() is None and database._read_cache.get() is None
    finally:
        runtime.close()
