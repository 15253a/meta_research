"""Repeated exact asset scope proofs share only their immutable read cut."""
from copy import copy

import pytest
from sqlalchemy import text

from meta_research.owners.common import OwnerConflict
from meta_research.read_snapshot_cache import snapshot_file_check
from test_research_datasets import _asset, _quest, runtime


def _accepted(runtime):
    graph = runtime.owners.research_graph
    quest = _quest(runtime, 'scope-cache')
    binding = _asset(runtime)
    graph.accept_asset_role(binding=binding, role='evidence', quest_ref=quest.quest_ref,
                            idempotency_key='scope-origin')
    return graph, quest, binding


def test_scope_proof_reuses_only_same_owner_version_and_quest_in_one_cut(runtime, monkeypatch):
    graph, quest, binding = _accepted(runtime)
    database = runtime._database
    other = _asset(runtime, content=b'Independent evidence.', key='other')
    graph.accept_asset_role(binding=other, role='evidence', quest_ref=quest.quest_ref,
                            idempotency_key='other-origin')
    read = graph.query_asset_roles
    calls = []

    def counted(**arguments):
        calls.append(arguments)
        return read(**arguments)

    monkeypatch.setattr(graph, 'query_asset_roles', counted)
    other_owner = copy(graph)
    with database.read_snapshot():
        first = graph.verify_asset_quest_scope(binding.version_ref, quest_ref=quest.quest_ref)
        second = graph.verify_asset_quest_scope(binding.version_ref, quest_ref=quest.quest_ref)
        assert first == second == binding
        assert first is not second
        assert len(calls) == 1
        assert graph.verify_asset_quest_scope(other.version_ref, quest_ref=quest.quest_ref) == other
        assert len(calls) == 2
        assert other_owner.verify_asset_quest_scope(binding.version_ref, quest_ref=quest.quest_ref) == binding
        assert len(calls) == 3
        for _ in range(2):
            with pytest.raises(OwnerConflict, match='asset_quest_scope_invalid'):
                graph.verify_asset_quest_scope(binding.version_ref, quest_ref='foreign-quest')
        assert len(calls) == 5  # Different Quest and failures are never shared.
    assert database._read_cut.get() is None and database._read_cache.get() is None
    for _ in range(2):
        assert graph.verify_asset_quest_scope(binding.version_ref, quest_ref=quest.quest_ref) == binding
    assert len(calls) == 7  # Outside a cut, every call retains the original verification.


def test_scope_proof_revalidates_changed_database_after_cut(runtime):
    graph, quest, binding = _accepted(runtime)
    database = runtime._database
    with database.read_snapshot():
        assert graph.verify_asset_quest_scope(binding.version_ref, quest_ref=quest.quest_ref) == binding
        assert graph.verify_asset_quest_scope(binding.version_ref, quest_ref=quest.quest_ref) == binding
    with database.write() as connection:
        connection.execute(text('UPDATE rg_asset_roles SET receipt_hash=:hash WHERE version_ref=:ref'),
                           {'ref': binding.version_ref, 'hash': '0' * 64})
    with database.read_snapshot():
        with pytest.raises(OwnerConflict, match='asset_role_receipt_invalid'):
            graph.verify_asset_quest_scope(binding.version_ref, quest_ref=quest.quest_ref)
    with pytest.raises(OwnerConflict, match='asset_role_receipt_invalid'):
        graph.verify_asset_quest_scope(binding.version_ref, quest_ref=quest.quest_ref)
    assert database._read_cut.get() is None and database._read_cache.get() is None


def test_scope_cache_hit_replays_transitive_file_check_and_does_not_cache_failure(runtime, tmp_path, monkeypatch):
    graph, quest, binding = _accepted(runtime)
    source = tmp_path / 'live-proof.txt'
    source.write_text('valid')
    read = graph.query_asset_roles
    calls = []

    @snapshot_file_check
    def verify_source():
        if source.read_text() != 'valid':
            raise OwnerConflict('live_file_changed')

    def checked(**arguments):
        calls.append(arguments)
        verify_source()
        return read(**arguments)

    monkeypatch.setattr(graph, 'query_asset_roles', checked)
    with runtime._database.read_snapshot():
        assert graph.verify_asset_quest_scope(binding.version_ref, quest_ref=quest.quest_ref) == binding
        source.write_text('changed')
        with pytest.raises(OwnerConflict, match='live_file_changed'):
            graph.verify_asset_quest_scope(binding.version_ref, quest_ref=quest.quest_ref)
        source.write_text('valid')
        assert graph.verify_asset_quest_scope(binding.version_ref, quest_ref=quest.quest_ref) == binding
        assert len(calls) == 1
