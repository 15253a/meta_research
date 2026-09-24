from meta_research.database import Database
from meta_research.owners.common import OwnerConflict
from meta_research.read_snapshot_cache import snapshot_cached
import pytest


class Reader:
    def __init__(self, database):
        self._database = database
        self.calls = 0

    @snapshot_cached
    def query(self, key, *, filters=None):
        self.calls += 1
        if not isinstance(key, str):
            raise OwnerConflict('reader_key_invalid')
        return {'key': key, 'filters': filters, 'items': []}


def test_snapshot_cache_bypasses_unhashable_inputs_without_changing_query_errors(tmp_path):
    database = Database(tmp_path / 'cache.sqlite3')
    reader = Reader(database)
    reader.query('same')
    reader.query('same')
    assert reader.calls == 2
    with database.read_snapshot():
        first = reader.query('same')
        first['items'].append('mutation')
        assert reader.query('same')['items'] == []
        assert reader.calls == 3
        for _repeat in range(2):
            with pytest.raises(OwnerConflict, match='reader_key_invalid'):
                reader.query([])
        assert reader.calls == 5
        assert reader.query('same', filters={'a': 1})['filters'] == {'a': 1}
        assert reader.query('same', filters={'a': 2})['filters'] == {'a': 2}
        assert reader.calls == 7
    assert database._read_cut.get() is None and database._read_cache.get() is None
    reader.query('same')
    assert reader.calls == 8
