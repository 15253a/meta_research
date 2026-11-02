"""Slow native deletion yields the Owner writer for the next sweep."""
import json
import sqlite3
import sys
from uuid import uuid4

from meta_research.native_session_cleanup import NativeSessionCleanup, delete_codex_sessions
from meta_research.paths import prepare_data_root


def _seed_threads(root, refs):
    with sqlite3.connect(root.codex_home / 'state_5.sqlite') as db:
        db.executescript("""
            CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT);
            CREATE TABLE thread_spawn_edges (parent_thread_id TEXT, child_thread_id TEXT, status TEXT);
            CREATE TABLE backfill_state (status TEXT);
            INSERT INTO backfill_state VALUES ('complete');
        """)
        for ref in refs:
            path = root.codex_archived_sessions / f'rollout-2026-11-02-{ref}.jsonl'
            path.write_text(json.dumps({'type': 'session_meta', 'payload': {'id': ref}}) + '\n')
            db.execute('INSERT INTO threads VALUES (?,?)', (ref, str(path)))


def test_slow_harness_does_not_wait_once_per_retired_thread(tmp_path):
    root = prepare_data_root(tmp_path / 'isolated')
    refs = [str(uuid4()) for _ in range(3)]
    _seed_threads(root, refs)
    calls = root.root / 'calls.txt'
    harness = tmp_path / 'stalled-harness'
    harness.write_text(f'#!{sys.executable}\n'
                       'import sys, time\n'
                       'with open("calls.txt", "a") as log: print(sys.argv[-1], file=log)\n'
                       'time.sleep(60)\n')
    harness.chmod(0o700)
    outcome = delete_codex_sessions(root, str(harness), refs, dry_run=False)
    assert len(calls.read_text().splitlines()) == 1
    assert {item['session_ref'] for item in outcome} == set(refs)
    assert all(item['action'] == 'skipped' and item['reason'] == 'native_cleanup_deferred' for item in outcome)
    assert len(list(root.codex_archived_sessions.glob('*.jsonl'))) == 3

def test_slow_leaf_yields_to_healthy_sibling_across_independent_cleanup_pages(tmp_path):
    root = prepare_data_root(tmp_path / 'isolated')
    refs = sorted(str(uuid4()) for _ in range(5))
    slow_a, healthy_a, slow_b, healthy_b, unrelated = refs
    _seed_threads(root, refs)
    harness = tmp_path / 'sometimes-stalled-harness'
    harness.write_text(f'#!{sys.executable}\n' + f"""
import os, sqlite3, sys, time
from pathlib import Path

ref = sys.argv[-1]
with open("calls.txt", "a") as log:
    print(ref, file=log)
if ref in {repr((slow_a, slow_b))}:
    time.sleep(60)
with sqlite3.connect(Path(os.environ["CODEX_HOME"]) / "state_5.sqlite") as db:
    path, = db.execute("SELECT rollout_path FROM threads WHERE id=?", (ref,)).fetchone()
    Path(path).unlink()
    db.execute("DELETE FROM threads WHERE id=?", (ref,))
""")
    harness.chmod(0o700)
    cleanup = NativeSessionCleanup(root, str(harness), None)
    page_a, page_b = [slow_a, healthy_a], [slow_b, healthy_b]
    for page in (page_a, page_b):
        records = cleanup.delete(page, False, ())
        assert all(r['reason'] == 'native_cleanup_deferred' for r in records)

    # Real cleanup also visits other pages/long-lived roots between sweeps.
    assert cleanup.delete([unrelated], False, ())[0]['action'] == 'removed'
    assert all(r['action'] == 'candidate' for r in cleanup.delete(page_a, True, ()))
    for page in (page_a, page_b):
        slow, healthy = page
        records = {r['session_ref']: r for r in cleanup.delete(page, False, ())}
        assert records[healthy]['action'] == 'removed'
        assert records[slow]['reason'] == 'native_cleanup_deferred'
    with sqlite3.connect(root.codex_home / 'state_5.sqlite') as db:
        assert {ref for ref, in db.execute("SELECT id FROM threads")} == {slow_a, slow_b}
    assert {p.name for p in root.codex_archived_sessions.iterdir()} == {
        f'rollout-2026-11-02-{ref}.jsonl' for ref in (slow_a, slow_b)}
