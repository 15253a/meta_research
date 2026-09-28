"""A cached database proof must retain its exact live file dependencies."""
import pytest

from meta_research.database import Database
from meta_research import read_snapshot_cache
from meta_research.read_snapshot_cache import snapshot_cached, snapshot_file_check


@snapshot_file_check
def _check_file(path, expected):
    if path.read_text() != expected:
        raise ValueError('changed_file')


class _Owner:
    def __init__(self, database):
        self._database = database
        self.calls = 0

    @snapshot_cached
    def leaf(self, path):
        self.calls += 1
        _check_file(path, 'accepted')
        return {'values': []}

    @snapshot_cached
    def parent(self, path):
        return self.leaf(path)

    @snapshot_cached
    def failing(self, path):
        self.leaf(path)
        raise ValueError('proof_failed')


def test_nested_cache_hit_propagates_only_its_file_dependencies(tmp_path):
    database = Database(tmp_path / 'state.sqlite3')
    owner = _Owner(database)
    first, unrelated = tmp_path / 'first', tmp_path / 'unrelated'
    first.write_text('accepted')
    unrelated.write_text('accepted')
    try:
        with database.read_snapshot():
            # Parent must acquire dependencies even when its child is a hit.
            owner.leaf(first)
            result = owner.parent(first)
            result['values'].append('caller mutation')
            assert owner.parent(first) == {'values': []}
            assert owner.calls == 1
            first.write_text('tampered')
            with pytest.raises(ValueError, match='changed_file'):
                owner.parent(first)
            # Neither a failed hit nor an unrelated damaged file poisons reads.
            assert owner.parent(unrelated) == {'values': []}
            first.write_text('accepted')
            assert owner.parent(first) == {'values': []}
            assert owner.calls == 2
        assert database._read_cache.get() is None
    finally:
        database.close()


def test_dependency_collector_is_released_after_failed_proof(tmp_path):
    database = Database(tmp_path / 'state.sqlite3')
    owner = _Owner(database)
    first, unrelated = tmp_path / 'first', tmp_path / 'unrelated'
    first.write_text('accepted')
    unrelated.write_text('accepted')
    try:
        with database.read_snapshot():
            with pytest.raises(ValueError, match='proof_failed'):
                owner.failing(first)
            assert read_snapshot_cache._file_checks.get() is None
            assert owner.parent(unrelated) == {'values': []}
            first.write_text('tampered')
            assert owner.parent(unrelated) == {'values': []}
        first.write_text('accepted')
        with database.read_snapshot():
            assert owner.parent(first) == {'values': []}
        assert database._read_cache.get() is None
        assert read_snapshot_cache._file_checks.get() is None
    finally:
        database.close()
