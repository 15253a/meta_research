"""Slow native deletion yields the Owner writer for the next sweep."""
import json
import sqlite3
import sys
from uuid import uuid4

from meta_research.native_session_cleanup import delete_codex_sessions
from meta_research.paths import prepare_data_root


def test_slow_harness_does_not_wait_once_per_retired_thread(tmp_path):
    root = prepare_data_root(tmp_path / 'isolated')
    refs = [str(uuid4()) for _ in range(3)]
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
