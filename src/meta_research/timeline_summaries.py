"""Persistent, independently generated reading notes for the research timeline.

These are display summaries, never research facts or workflow prerequisites.
The worker owns discovery and generation; HTTP reads cannot enqueue model work.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable

from meta_research.owners.common import canonical_hash


SCHEMA = "meta-research/timeline-summaries/v1"
MAX_BATCH_NODES = 20
MAX_BATCH_BYTES = 240_000
GENERATION_TIMEOUT_SECONDS = 900
DEFAULT_SCAN_SECONDS = 300
MAX_SCAN_SECONDS = 7200
# Version the prose policy independently from scheduling instructions. Preserve
# the original policy hash so a cadence-only update does not regenerate history.
# Change this value when the factual summarization policy itself changes.
SUMMARY_CONTENT_VERSION = '8e2dd45bb4551a37755fd8e60b7bcbfb9f2e00eda2fa1f90c0bf78c6eae16834'


def _scan_interval(value):
    return value if type(value) is int and DEFAULT_SCAN_SECONDS <= value <= MAX_SCAN_SECONDS else DEFAULT_SCAN_SECONDS


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class TimelineSummaryStore:
    def __init__(self, path: Path):
        self.path = path
        self._initialized = False
        self._lock = threading.RLock()

    @contextmanager
    def _write(self):
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(self.path, timeout=5) as db:
                db.row_factory = sqlite3.Row
                if not self._initialized:
                    db.execute("PRAGMA journal_mode=WAL")
                    db.executescript("""
                        CREATE TABLE IF NOT EXISTS quests (
                            quest_ref TEXT PRIMARY KEY, revision INTEGER NOT NULL DEFAULT 0,
                            native_session_ref TEXT, observed_at REAL NOT NULL DEFAULT 0
                        );
                        CREATE TABLE IF NOT EXISTS nodes (
                            quest_ref TEXT NOT NULL, node_key TEXT NOT NULL,
                            source_hash TEXT NOT NULL, source_revision INTEGER NOT NULL,
                            node_json TEXT NOT NULL, summary TEXT, summary_source_hash TEXT,
                            summary_source_revision INTEGER NOT NULL DEFAULT 0,
                            summary_sources_json TEXT NOT NULL DEFAULT '[]', updated_at REAL,
                            status TEXT NOT NULL, retry_at REAL NOT NULL DEFAULT 0,
                            failures INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1,
                            PRIMARY KEY (quest_ref, node_key)
                        );
                        CREATE TABLE IF NOT EXISTS jobs (
                            job_ref TEXT PRIMARY KEY, quest_ref TEXT NOT NULL,
                            request_json TEXT NOT NULL, status TEXT NOT NULL,
                            created_at REAL NOT NULL, error_code TEXT,
                            retry_at REAL NOT NULL DEFAULT 0
                        );
                        CREATE UNIQUE INDEX IF NOT EXISTS one_active_job_per_quest
                            ON jobs(quest_ref) WHERE status='active';
                    """)
                    if 'retry_at' not in {row['name'] for row in db.execute('PRAGMA table_info(jobs)')}:
                        db.execute('ALTER TABLE jobs ADD COLUMN retry_at REAL NOT NULL DEFAULT 0')
                    columns = {row['name'] for row in db.execute('PRAGMA table_info(quests)')}
                    if 'scan_interval_seconds' not in columns:
                        db.execute('ALTER TABLE quests ADD COLUMN scan_interval_seconds INTEGER NOT NULL DEFAULT 300')
                    if 'next_scan_at' not in columns:
                        db.execute('ALTER TABLE quests ADD COLUMN next_scan_at REAL NOT NULL DEFAULT 0')
                        db.execute('UPDATE quests SET next_scan_at=observed_at+300 WHERE observed_at>0')
                    if 'scan_decision_observed_at' not in columns:
                        db.execute('ALTER TABLE quests ADD COLUMN scan_decision_observed_at REAL NOT NULL DEFAULT -1')
                    self._initialized = True
                yield db

    @staticmethod
    def _bump(db, quest_ref: str):
        db.execute("UPDATE quests SET revision=revision+1 WHERE quest_ref=?", (quest_ref,))

    def sync(self, quest_ref: str, nodes: list[dict], now: float) -> bool:
        """Replace the observed source cut, preserving prior generated sentences."""
        with self._write() as db:
            db.execute("INSERT OR IGNORE INTO quests(quest_ref) VALUES (?)", (quest_ref,))
            previous = {row['node_key']: row for row in db.execute(
                "SELECT * FROM nodes WHERE quest_ref=?", (quest_ref,))}
            changed = False
            seen = set()
            for node in nodes:
                key = node['node_key']
                if key in seen:
                    raise ValueError("timeline_summary_duplicate_node")
                seen.add(key)
                old = previous.get(key)
                if old is None:
                    db.execute("""INSERT INTO nodes
                        (quest_ref,node_key,source_hash,source_revision,node_json,status)
                        VALUES (?,?,?,1,?,'pending')""",
                        (quest_ref, key, node['source_hash'], _json(node)))
                    changed = True
                elif old['source_hash'] != node['source_hash'] or not old['active']:
                    db.execute("""UPDATE nodes SET source_hash=?,source_revision=source_revision+1,
                        node_json=?,status='pending',retry_at=0,failures=0,active=1
                        WHERE quest_ref=? AND node_key=?""",
                        (node['source_hash'], _json(node), quest_ref, key))
                    changed = True
            for key, old in previous.items():
                if key not in seen and old['active']:
                    db.execute("UPDATE nodes SET active=0 WHERE quest_ref=? AND node_key=?", (quest_ref, key))
                    changed = True
            db.execute("UPDATE quests SET observed_at=? WHERE quest_ref=?", (now, quest_ref))
            if changed:
                self._bump(db, quest_ref)
            return changed

    def claim(self, now: float, language: str) -> dict | None:
        with self._write() as db:
            active = db.execute("SELECT * FROM jobs WHERE status='active' AND retry_at<=? ORDER BY created_at LIMIT 1", (now,)).fetchone()
            if active is not None:
                return {**dict(active), **json.loads(active['request_json'])}
            first = db.execute("""SELECT quest_ref FROM nodes WHERE active=1 AND retry_at<=?
                AND status IN ('pending','failed')
                AND NOT EXISTS (SELECT 1 FROM jobs WHERE jobs.quest_ref=nodes.quest_ref AND jobs.status='active')
                ORDER BY retry_at,rowid LIMIT 1""", (now,)).fetchone()
            if first is None:
                return None
            quest_ref = first['quest_ref']
            selected, size = [], 0
            for row in db.execute("""SELECT * FROM nodes WHERE quest_ref=? AND active=1
                AND retry_at<=? AND status IN ('pending','failed') ORDER BY rowid""", (quest_ref, now)):
                node = json.loads(row['node_json'])
                node.update(previous_summary=row['summary'], source_revision=row['source_revision'])
                node_size = len(_json(node).encode('utf-8'))
                if selected and (len(selected) >= MAX_BATCH_NODES or size + node_size > MAX_BATCH_BYTES):
                    break
                selected.append(node)
                size += node_size
            quest = db.execute('SELECT native_session_ref,observed_at FROM quests WHERE quest_ref=?', (quest_ref,)).fetchone()
            request = {'nodes': selected, 'native_session_ref': quest['native_session_ref'],
                       'observed_at': quest['observed_at'], 'output_language': language}
            job_ref = 'timeline-summary-' + uuid.uuid4().hex
            db.execute("INSERT INTO jobs(job_ref,quest_ref,request_json,status,created_at) VALUES (?,?,?,'active',?)",
                       (job_ref, quest_ref, _json(request), now))
            for node in selected:
                db.execute("UPDATE nodes SET status='updating' WHERE quest_ref=? AND node_key=?", (quest_ref, node['node_key']))
            self._bump(db, quest_ref)
            return {'job_ref': job_ref, 'quest_ref': quest_ref, 'created_at': now, **request}

    def defer(self, job: dict, until: float):
        """Retain the exact durable job while allowing other Quests to proceed."""
        with self._write() as db:
            db.execute("UPDATE jobs SET retry_at=? WHERE job_ref=? AND status='active'", (until, job['job_ref']))

    def scan_schedule(self, quest_ref: str) -> tuple[float, int]:
        with self._write() as db:
            row = db.execute('SELECT next_scan_at,scan_interval_seconds FROM quests WHERE quest_ref=?', (quest_ref,)).fetchone()
            return (row['next_scan_at'], _scan_interval(row['scan_interval_seconds'])) if row else (0, DEFAULT_SCAN_SECONDS)

    def schedule_scan(self, quest_ref: str, now: float, seconds: int | None = None) -> float:
        with self._write() as db:
            db.execute('INSERT OR IGNORE INTO quests(quest_ref) VALUES (?)', (quest_ref,))
            if seconds is None:
                seconds = db.execute('SELECT scan_interval_seconds FROM quests WHERE quest_ref=?', (quest_ref,)).fetchone()[0]
            seconds = _scan_interval(seconds)
            db.execute('UPDATE quests SET next_scan_at=?,scan_interval_seconds=? WHERE quest_ref=?', (now + seconds, seconds, quest_ref))
            return now + seconds

    def publish(self, job: dict, summaries: list[dict], native_session_ref: str, now: float,
                next_scan_seconds: int = DEFAULT_SCAN_SECONDS):
        with self._write() as db:
            state = db.execute("SELECT status FROM jobs WHERE job_ref=?", (job['job_ref'],)).fetchone()
            if state is None or state['status'] != 'active':
                return
            requested = {node['node_key']: node for node in job['nodes']}
            for result in summaries:
                node = requested[result['node_key']]
                current = db.execute("SELECT * FROM nodes WHERE quest_ref=? AND node_key=?",
                                     (job['quest_ref'], node['node_key'])).fetchone()
                if current is None or not current['active'] or node['source_revision'] < current['summary_source_revision']:
                    continue
                # A useful first draft may finish while work progresses. Keep its
                # exact provenance and leave it pending until the newer cut is summarized.
                latest = current['source_hash'] == result['source_hash']
                refs = set(result['source_refs'])
                sources = [source for source in node['sources'] if source['ref'] in refs]
                db.execute("""UPDATE nodes SET summary=?,summary_source_hash=?,summary_source_revision=?,
                    summary_sources_json=?,updated_at=?,status=?,retry_at=0,failures=0
                    WHERE quest_ref=? AND node_key=?""",
                    (result['summary'], result['source_hash'], node['source_revision'], _json(sources), now,
                     'ready' if latest else 'pending', job['quest_ref'], node['node_key']))
            seconds = _scan_interval(next_scan_seconds)
            quest = db.execute('SELECT * FROM quests WHERE quest_ref=?', (job['quest_ref'],)).fetchone()
            observation = job.get('observed_at', quest['observed_at'])
            same_cut = observation == quest['observed_at']
            first_decision = same_cut and quest['scan_decision_observed_at'] != observation
            if first_decision:
                deadline = now + seconds
            else:
                # Several batches may summarize the same scan. A later batch
                # about old work cannot postpone an earlier urgent decision.
                seconds = min(seconds, _scan_interval(quest['scan_interval_seconds']))
                deadline = min(quest['next_scan_at'], now + seconds)
            decision_cut = observation if same_cut else quest['scan_decision_observed_at']
            db.execute('''UPDATE quests SET native_session_ref=?,scan_interval_seconds=?,next_scan_at=?,
                scan_decision_observed_at=? WHERE quest_ref=?''',
                (native_session_ref, seconds, deadline, decision_cut, job['quest_ref']))
            db.execute("UPDATE jobs SET status='completed',request_json='{}' WHERE job_ref=?", (job['job_ref'],))
            self._bump(db, job['quest_ref'])

    def fail(self, job: dict, code: str, now: float, native_session_ref: str | None = None):
        with self._write() as db:
            state = db.execute("SELECT status FROM jobs WHERE job_ref=?", (job['job_ref'],)).fetchone()
            if state is None or state['status'] != 'active':
                return
            for node in job['nodes']:
                current = db.execute("SELECT source_hash,failures FROM nodes WHERE quest_ref=? AND node_key=?",
                                     (job['quest_ref'], node['node_key'])).fetchone()
                if current is None or current['source_hash'] != node['source_hash']:
                    continue
                failures = current['failures'] + 1
                db.execute("""UPDATE nodes SET status='failed',failures=?,retry_at=?
                    WHERE quest_ref=? AND node_key=?""",
                    (failures, now + min(MAX_SCAN_SECONDS, DEFAULT_SCAN_SECONDS * 2 ** min(failures - 1, 5)), job['quest_ref'], node['node_key']))
            if native_session_ref:
                db.execute("UPDATE quests SET native_session_ref=? WHERE quest_ref=?", (native_session_ref, job['quest_ref']))
            db.execute("UPDATE jobs SET status='failed',error_code=?,request_json='{}' WHERE job_ref=?", (code, job['job_ref']))
            self._bump(db, job['quest_ref'])

    def query(self, quest_ref: str) -> dict:
        result = {'schema_ref': SCHEMA, 'quest_ref': quest_ref, 'revision': 0, 'observed_at': 0, 'nodes': []}
        if not self.path.exists():
            return result
        # Read-only URI: opening the page never creates tables, jobs, or summaries.
        with sqlite3.connect(self.path.resolve().as_uri() + '?mode=ro', uri=True, timeout=2) as db:
            db.row_factory = sqlite3.Row
            db.execute('BEGIN')
            quest = db.execute("SELECT * FROM quests WHERE quest_ref=?", (quest_ref,)).fetchone()
            if quest is None:
                return result
            result.update({key: quest[key] for key in ('revision', 'observed_at', 'scan_interval_seconds', 'next_scan_at') if key in quest.keys()})
            for row in db.execute("SELECT * FROM nodes WHERE quest_ref=? AND active=1 ORDER BY rowid", (quest_ref,)):
                node = json.loads(row['node_json'])
                result['nodes'].append({
                    **{key: node.get(key) for key in ('node_key','kind','question_ref','cycle_ref','stage','target_ref')},
                    'summary': row['summary'], 'status': row['status'], 'source_hash': row['source_hash'],
                    'summarized_source_hash': row['summary_source_hash'], 'updated_at': row['updated_at'],
                    'sources': json.loads(row['summary_sources_json']),
                })
        return result


class TimelineSummaryService:
    def __init__(self, store: TimelineSummaryStore, reader: Any, provider_factory: Callable[[], Any], *,
                 clock: Callable[[], float] = time.time, language: Callable[[], str] = lambda: 'zh',
                 prompt_version: Callable[[], str] = lambda: 'research-recorder-v1'):
        self.store, self.reader = store, reader
        self._provider_factory = provider_factory
        self._provider = None
        self._clock, self._language, self._prompt_version = clock, language, prompt_version
        self._next_scan: dict[str, float] = {}
        self._operation_lock = threading.Lock()
        self._stopped = False
        self.last_error: str | None = None

    def query(self, quest_ref: str) -> dict:
        return self.store.query(quest_ref)

    def _observe(self, quest_ref: str, now: float):
        # Reserve the next scan before I/O, so a failed post-generation read
        # cannot turn a publication retry into repeated source scans.
        self._next_scan[quest_ref] = self.store.schedule_scan(quest_ref, now)
        nodes = self.reader.read(quest_ref)
        version = {'language': self._language(), 'prompt': self._prompt_version()}
        for node in nodes:
            node['source_hash'] = canonical_hash({'source': node['source_hash'], **version})
        changed = self.store.sync(quest_ref, nodes, now)
        return changed

    def _finish(self, job: dict):
        # Provenance and prose are already durable. Retaining full model spools
        # for every automatic update would duplicate the research indefinitely.
        finish = getattr(self._provider, 'finish_job', None)
        if callable(finish):
            try:
                finish(job['job_ref'])
            except Exception:
                pass  # Cleanup must never undo a published summary.

    def process_once(self) -> bool:
        if self._stopped or not self._operation_lock.acquire(blocking=False):
            return False
        try:
            now = self._clock()
            for quest_ref in self.reader.quest_refs():
                if quest_ref not in self._next_scan:
                    self._next_scan[quest_ref] = self.store.scan_schedule(quest_ref)[0]
                if self._next_scan[quest_ref] <= now:
                    try:
                        self._observe(quest_ref, now)
                    except Exception as error:
                        # One temporarily unreadable project must not starve
                        # the recorder's other projects or its durable jobs.
                        self.last_error = getattr(error, 'code', 'timeline_summary_source_unavailable')
                        self._next_scan[quest_ref] = self.store.schedule_scan(quest_ref, now, DEFAULT_SCAN_SECONDS)
            job = self.store.claim(now, self._language())
            if job is None or self._stopped:
                return False
            try:
                if self._provider is None:
                    self._provider = self._provider_factory()
                state = self._provider.reconcile_job(job['job_ref'])
                if state not in {'absent', 'terminal'}:
                    if now - job['created_at'] > GENERATION_TIMEOUT_SECONDS + 60 and self._provider.cancel_job(job['job_ref']):
                        self.store.fail(job, 'timeline_summary_generation_interrupted', now)
                        self._finish(job)
                    else:
                        self.store.defer(job, now + 30)
                    return False
                if state == 'terminal':
                    summaries, native, next_scan_seconds = self._provider.recover_summaries(job)
                else:
                    summaries, native, next_scan_seconds = self._provider.summarize(
                        quest_ref=job['quest_ref'], nodes=job['nodes'], native_session_ref=job['native_session_ref'],
                        job_ref=job['job_ref'], output_language=job['output_language'])
                if self._stopped:
                    return False  # Durable provider result will be recovered on the next startup.
            except Exception as error:
                code = getattr(error, 'code', 'timeline_summary_generation_unavailable')
                self.last_error = code
                # A lost response is not proof that the durable provider has
                # stopped. Retain its exact job until verified terminal/absent;
                # otherwise retry could overlap the same native Session.
                if self._provider is not None:
                    try:
                        if self._provider.reconcile_job(job['job_ref']) not in {'absent', 'terminal'}:
                            self.store.defer(job, self._clock() + 30)
                            return False
                    except Exception:
                        self.store.defer(job, self._clock() + 30)
                        return False
                self.store.fail(job, code, self._clock(), getattr(error, 'native_session_ref', None))
                self._finish(job)
                return False
            try:
                # Observe only when due, including across restarts and batches.
                # Every sentence retains the exact snapshot it summarizes.
                # Keep the durable job active if collection/storage is down:
                # the provider replays the finished result without a new call.
                if self._next_scan.get(job['quest_ref'], 0) <= self._clock():
                    self._observe(job['quest_ref'], self._clock())
                self.store.publish(job, summaries, native, self._clock(), next_scan_seconds)
                self._next_scan[job['quest_ref']] = self.store.scan_schedule(job['quest_ref'])[0]
            except Exception as error:
                self.last_error = getattr(error, 'code', 'timeline_summary_publication_unavailable')
                self.store.defer(job, self._clock() + 30)
                return False
            self.last_error = None
            self._finish(job)
            return True
        finally:
            self._operation_lock.release()

    def request_stop(self):
        self._stopped = True
        if self._provider is not None:
            self._provider.request_stop()


def create_timeline_summary_service(runtime, executable: str, *, provider=None) -> TimelineSummaryService:
    from meta_research.timeline_summary_sources import TimelineSummarySourceReader
    from meta_research.system_prompt import read_output_language

    root = runtime.data_root.root / 'research-recorder'

    def create_provider():
        from meta_research.quest_drafting import _CancellableProcessRunner
        from meta_research.timeline_summary_provider import CodexTimelineSummaryAdapter
        return provider or CodexTimelineSummaryAdapter(
            root / 'provider', executable=executable, timeout_seconds=GENERATION_TIMEOUT_SECONDS,
            process_runner=_CancellableProcessRunner(protected_environment=runtime.data_root.codex_environment))

    def prompt_version():
        return SUMMARY_CONTENT_VERSION

    return TimelineSummaryService(TimelineSummaryStore(root / 'summaries.sqlite3'),
        TimelineSummarySourceReader(runtime), create_provider,
        language=lambda: read_output_language(runtime.data_root.root), prompt_version=prompt_version)
