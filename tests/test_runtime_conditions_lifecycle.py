from concurrent.futures import ThreadPoolExecutor
import threading

import pytest
from sqlalchemy import text, event

from meta_research.composition import build_production_runtime
from meta_research.paths import prepare_data_root
from meta_research.owners.common import OwnerConflict
from meta_research.runtime_conditions import read_runtime_conditions, save_runtime_conditions, read_runtime_conditions_in_transaction
from test_corrected_quest_initialization import DeterministicDraftingAdapter, DeterministicProbe, _ready_v2


@pytest.fixture
def ready_runtime(tmp_path):
    adapter = DeterministicDraftingAdapter()
    runtime = build_production_runtime(prepare_data_root(tmp_path / "conditions"),
        proposal_drafter=adapter, intent_drafting_provider=adapter, host_compute_probe=DeterministicProbe())
    try:
        yield runtime, _ready_v2(runtime, "conditions-lifecycle")
    finally:
        runtime.close()


def _confirm(runtime, ready):
    hc = runtime.owners.human_collaboration
    hc.confirm_quest(ready['initialization_id'], quest_draft_revision=ready['quest_draft']['revision'],
        quest_draft_hash=ready['quest_draft']['hash'], proposal_ref=ready['proposal']['ref'],
        proposal_hash=ready['proposal']['hash'], preview_ref=ready['confirmation_preview']['ref'],
        preview_hash=ready['confirmation_preview']['hash'], idempotency_key='conditions-accept')
    for _ in range(8):
        hc.reconcile_once()
    return runtime.owners.research_graph.query_quest(ready['initialization_id'])


def test_initialization_reads_do_not_write_and_default_follows_draft(ready_runtime):
    runtime, ready = ready_runtime
    scope = 'initialization:' + ready['initialization_id']
    statements = []
    def record(_conn, _cursor, statement, _params, _context, _many):
        statements.append(statement)
    event.listen(runtime._database._engine, 'before_cursor_execute', record)
    try:
        original = read_runtime_conditions(runtime.data_root.root, scope)
        assert read_runtime_conditions(runtime.data_root.root, scope) == original
    finally:
        event.remove(runtime._database._engine, 'before_cursor_execute', record)
    assert not any(sql.lstrip().upper().startswith(('INSERT', 'UPDATE', 'DELETE', 'BEGIN IMMEDIATE')) for sql in statements)
    with runtime._database.read() as connection:
        assert connection.execute(text('SELECT COUNT(*) FROM hc_runtime_condition_versions')).scalar_one() == 0
    hc = runtime.owners.human_collaboration
    hc.revise_quest_draft(ready['initialization_id'],
        draft={**ready['quest_draft']['value'], 'goal': 'Updated research goal'},
        expected_draft_revision=ready['quest_draft']['revision'],
        expected_draft_hash=ready['quest_draft']['hash'], idempotency_key='conditions-edit')
    edited = read_runtime_conditions(runtime.data_root.root, scope)
    assert edited['revision'] != original['revision']
    assert edited['text'] == original['text']
    with pytest.raises(OwnerConflict, match='runtime_conditions_stale'):
        save_runtime_conditions(runtime.data_root.root, scope, text='stale edit', expected_revision=original['revision'])


def test_concurrent_first_save_has_one_winner(ready_runtime):
    runtime, ready = ready_runtime
    scope = 'initialization:' + ready['initialization_id']
    original = read_runtime_conditions(runtime.data_root.root, scope)
    barrier = threading.Barrier(2)
    def save(value):
        barrier.wait(timeout=10)
        try:
            return save_runtime_conditions(runtime.data_root.root, scope, text=value, expected_revision=original['revision'])
        except OwnerConflict as error:
            return error.code
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(save, ['first', 'second']))
    assert sum(isinstance(result, dict) for result in results) == 1
    assert 'runtime_conditions_stale' in results


@pytest.mark.parametrize('override', [False, True])
def test_acceptance_materializes_full_conditions_and_preserves_exact_history(ready_runtime, override):
    runtime, ready = ready_runtime
    scope = 'initialization:' + ready['initialization_id']
    current = read_runtime_conditions(runtime.data_root.root, scope)
    if override:
        current = save_runtime_conditions(runtime.data_root.root, scope, text='Human complete conditions',
            expected_revision=current['revision'], research_style='focus',
            stage_models={key: 'gpt-5.6-sol' for key in current['stage_models']})
    quest = _confirm(runtime, ready)
    assert quest is not None
    accepted = read_runtime_conditions(runtime.data_root.root, quest.quest_ref)
    for key in ('text', 'research_style', 'literature_configuration', 'stage_models'):
        assert accepted.get(key) == current.get(key)
    changed = save_runtime_conditions(runtime.data_root.root, quest.quest_ref,
        text='A newer human version', expected_revision=accepted['revision'])
    assert changed['revision'] != accepted['revision']
    with runtime._database.read_snapshot() as connection:
        historical = read_runtime_conditions_in_transaction(connection, quest.quest_ref, revision=accepted['revision'])
        assert historical['text'] == accepted['text']
    with pytest.raises(OwnerConflict, match='runtime_conditions_stale'):
        save_runtime_conditions(runtime.data_root.root, scope, text='stale initialization save', expected_revision=current['revision'])


def test_save_racing_acceptance_either_inherits_or_rejects_stale_scope(ready_runtime):
    runtime, ready = ready_runtime
    scope = 'initialization:' + ready['initialization_id']
    initial = read_runtime_conditions(runtime.data_root.root, scope)
    barrier = threading.Barrier(2)
    def save():
        barrier.wait(timeout=10)
        try:
            return save_runtime_conditions(runtime.data_root.root, scope, text='Concurrent human conditions', expected_revision=initial['revision'])
        except OwnerConflict as error:
            return error.code
    def accept():
        barrier.wait(timeout=10)
        return _confirm(runtime, ready)
    with ThreadPoolExecutor(2) as pool:
        saved = pool.submit(save)
        accepted = pool.submit(accept)
        save_result, quest = saved.result(), accepted.result()
    assert quest is not None
    current = read_runtime_conditions(runtime.data_root.root, quest.quest_ref)
    if isinstance(save_result, dict):
        assert current['text'] == 'Concurrent human conditions'
    else:
        assert save_result == 'runtime_conditions_stale'
        assert current['text'] == initial['text']
