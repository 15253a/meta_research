"""Reuse proofs stay shared during preparation, never across content writes."""
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace

import pytest
from sqlalchemy import text

from meta_research.owners import research_memory as module
from meta_research.owners.common import OwnerConflict
from test_public_reasoning_plan_evidence_reuse import (
    _EvidenceReuseAuthority, _MetricReuseReasoningSkill, _runtime,
    _confirm_direct_quest, _install_fixture_plan_evidence, _finish_idea_stage,
    _finish_plan_and_skipped_bundle,
)
from test_reasoning_acceptance_read_boundaries import pending_reasoning


def test_direct_reasoning_receipt_proof_uses_fresh_snapshots(pending_reasoning, monkeypatch):
    runtime, _quest, content = pending_reasoning
    graph, memory, database = runtime.owners.research_graph, runtime.owners.research_memory, runtime._database
    decision = graph.decide_reasoning_outcome(content=content)
    verify = module._verify_reasoning_plan_evidence_reuse_authority
    caches = []

    def inspect(context_pack, resolver):
        assert database._read_cut.get() is not None
        assert database._read_cache.get() is not None
        caches.append(database._read_cache.get())
        return verify(context_pack, resolver)

    monkeypatch.setattr(module, '_verify_reasoning_plan_evidence_reuse_authority', inspect)
    values = (content.request_ref, content.submission_ref, decision.decision, decision.outcome_ref, decision.receipt)
    direct = graph._receipt_verifier.verify_reasoning_outcome_decision
    direct(*values)
    first = caches[-1]
    direct(*values)
    assert first is not caches[-1]
    with database.read_snapshot() as outer:
        direct(*values)
        assert database._read_cut.get() is outer
        assert caches[-1] is database._read_cache.get()
    with pytest.raises(OwnerConflict):
        direct(*values[:-1], replace(decision.receipt, payload_hash='0' * 64))
    assert database._read_cut.get() is None and database._read_cache.get() is None
    # Existing writes use their own connection: like the original verifier's
    # reads, this proof sees committed rows, then refreshes after that commit.
    try:
        with database.write() as connection:
            connection.execute(text('UPDATE rm_reasoning_contents SET payload_hash=:value WHERE content_ref=:ref'),
                               {'value': '0' * 64, 'ref': content.content_ref})
            direct(*values)
            assert database._read_cut.get() is None and database._read_cache.get() is None
        with pytest.raises(OwnerConflict):
            direct(*values)
    finally:
        with database.write() as connection:
            connection.execute(text('UPDATE rm_reasoning_contents SET payload_hash=:value WHERE content_ref=:ref'),
                               {'value': content.payload_hash, 'ref': content.content_ref})
    direct(*values)


def test_content_acceptance_prepares_reused_evidence_before_fresh_write(tmp_path, monkeypatch):
    authority, provider = _EvidenceReuseAuthority(), _MetricReuseReasoningSkill()
    runtime = _runtime(tmp_path / 'reuse', authority, provider)
    try:
        quest = _confirm_direct_quest(runtime)
        _install_fixture_plan_evidence(runtime, authority, quest_ref=str(quest['quest_ref']))
        _finish_idea_stage(runtime)
        _finish_plan_and_skipped_bundle(runtime)
        memory, database = runtime.owners.research_memory, runtime._database
        accept, verify, write = memory.accept_reasoning_content, module._verify_reasoning_plan_evidence_reuse_authority, database.write
        accepting, cuts, writes = [], [], []

        def accept_content(**values):
            accepting.append(True)
            try:
                return accept(**values)
            finally:
                accepting.pop()

        def inspect(context_pack, resolver):
            if not accepting:
                return verify(context_pack, resolver)
            assert database._read_cut.get() is not None
            assert database._read_cache.get() is not None
            cuts.append(database._read_cache.get())
            original = deepcopy(context_pack)
            result = verify(context_pack, resolver)
            assert context_pack == original
            forged = deepcopy(context_pack)
            forged['plan_evidence_input']['evidence_reuse_closure'][0]['evidence_item_ref'] = 'wrong-source'
            with pytest.raises(OwnerConflict):
                verify(forged, resolver)
            return result

        @contextmanager
        def fresh_write():
            if accepting:
                assert database._read_cut.get() is None
                assert database._read_cache.get() is None
                writes.append(True)
            with write() as connection:
                yield connection

        monkeypatch.setattr(memory, 'accept_reasoning_content', accept_content)
        monkeypatch.setattr(module, '_verify_reasoning_plan_evidence_reuse_authority', inspect)
        monkeypatch.setattr(database, 'write', fresh_write)
        for _ in range(9):
            current = runtime.reasoning_stage.query_current()
            if current['reasoning_acceptance']['status'] == 'accepted':
                break
            assert runtime.reasoning_stage.process_once()
        assert current['reasoning_acceptance']['status'] == 'accepted'
        assert cuts and writes
        assert database._read_cut.get() is None and database._read_cache.get() is None
    finally:
        runtime.close()
