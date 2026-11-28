"""Wave 1A regression suite: S1a, S1b, S2a, D7, D8a."""
from __future__ import annotations

import asyncio
import gzip
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
from uuid import uuid4

import pytest
from sqlalchemy import text

from meta_research import native_codex_bridge as bridge_mod
from meta_research import native_session_cleanup as cleanup_mod
from meta_research import runtime_conditions as rc_mod
from meta_research import web as web_mod
from meta_research.harness import HarnessRuntime
from meta_research.owners.common import OwnerConflict, canonical_hash
from meta_research.paths import prepare_data_root
from meta_research.provider_supervisor import ensure_transport_key, write_transport_envelope
from test_harness_target_root import _Gateway, _TargetRootAdapter, _WorkspaceResolver
from test_harness_target_root_recovery import _owner_with_active_root


# ---------------------------------------------------------------------------
# S1a: Exponential backoff (30->60->120->240->480s) & auto-park on upstream
#      capacity/transient failures; lift 2.0s clamp on generic retries.
# ---------------------------------------------------------------------------
def test_s1a_upstream_capacity_backoff_schedule_and_auto_park(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded_sleeps: list[float] = []
    calls = 0
    parked_seen = asyncio.Event()
    changes: list[tuple[str, str | None, bool]] = []

    async def fake_sleep(delay: float) -> None:
        recorded_sleeps.append(delay)
        if getattr(health, "parked", False):
            parked_seen.set()
            raise asyncio.CancelledError()
        if len(recorded_sleeps) >= 8:
            raise asyncio.CancelledError()

    monkeypatch.setattr(web_mod.asyncio, "sleep", fake_sleep)

    def failing_op() -> bool:
        nonlocal calls
        calls += 1
        return False

    health = web_mod.ReconciliationHealth()

    def on_change() -> None:
        changes.append((health.status, health.last_error, bool(getattr(health, "parked", False))))

    async def exercise() -> None:
        with pytest.raises(asyncio.CancelledError):
            await web_mod._process_background_operation(
                failing_op,
                health=health,
                worker_label="bundle stage",
                timeout_code="bundle_stage_operation_timeout",
                timeout_seconds=None,
                on_health_change=on_change,
                transient_error=lambda: "upstream_capacity",
            )

    asyncio.run(exercise())

    # Consecutive upstream capacity failures must follow 30->60->120->240->480s,
    # then park cleanly on the 6th failure (exceeding threshold 5).
    assert calls == 6
    assert recorded_sleeps[:5] == [30.0, 60.0, 120.0, 240.0, 480.0]
    assert parked_seen.is_set()
    assert health.parked is True
    assert health.status == "unavailable"
    assert health.last_error == "worker_parked:upstream_capacity"
    assert isinstance(health.warning, str) and "upstream_capacity" in health.warning
    assert changes[-1] == ("unavailable", "worker_parked:upstream_capacity", True)


def test_s1a_upstream_backoff_resets_after_successful_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded_sleeps: list[float] = []
    # 3 capacity failures -> 1 success -> 2 capacity failures -> cancel
    sequence = [
        "upstream_capacity",
        "upstream_capacity",
        "upstream_capacity",
        None,
        "upstream_transient",
        "upstream_transient",
    ]
    idx = 0
    current_err: list[str | None] = [None]

    async def fake_sleep(delay: float) -> None:
        recorded_sleeps.append(delay)
        if idx >= len(sequence):
            raise asyncio.CancelledError()

    monkeypatch.setattr(web_mod.asyncio, "sleep", fake_sleep)

    def op() -> bool:
        nonlocal idx
        err = sequence[idx]
        idx += 1
        current_err[0] = err
        return err is None

    health = web_mod.ReconciliationHealth()

    async def exercise() -> None:
        with pytest.raises(asyncio.CancelledError):
            await web_mod._process_background_operation(
                op,
                health=health,
                worker_label="idea stage",
                timeout_code="idea_stage_operation_timeout",
                timeout_seconds=None,
                on_health_change=None,
                transient_error=lambda: current_err[0],
            )

    asyncio.run(exercise())

    assert recorded_sleeps == [30.0, 60.0, 120.0, 0, 30.0, 60.0]
    assert health.parked is False
    assert health.retry_count == 2


def test_s1a_generic_error_backoff_not_clamped_at_two_seconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded_sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        recorded_sleeps.append(delay)
        if len(recorded_sleeps) >= 6:
            raise asyncio.CancelledError()

    monkeypatch.setattr(web_mod.asyncio, "sleep", fake_sleep)

    def failing_op() -> bool:
        raise OwnerConflict("generic_local_fault")

    health = web_mod.ReconciliationHealth()

    async def exercise() -> None:
        with pytest.raises(asyncio.CancelledError):
            await web_mod._process_background_operation(
                failing_op,
                health=health,
                worker_label="quest reconciliation",
                timeout_code="quest_reconciliation_io_timeout",
                timeout_seconds=None,
                on_health_change=None,
            )

    asyncio.run(exercise())

    # First retry remains fast (0.2s) for existing tests, while subsequent
    # retries are no longer clamped at 2.0s.
    assert recorded_sleeps == pytest.approx([0.2, 0.4, 0.8, 1.6, 3.2, 6.4])
    assert max(recorded_sleeps) > 2.0


# ---------------------------------------------------------------------------
# S1b: native_codex_bridge error surfacing, idle watchdog, 64MB stdout cap
# ---------------------------------------------------------------------------
def test_s1b_bridge_surfaces_method_error_and_classifies_capacity(
    tmp_path: Path,
) -> None:
    if os.name != "posix":
        pytest.skip("The production durable provider process boundary runs on Linux")
    server = tmp_path / "capacity-codex"
    server.write_text(
        "#!" + sys.executable + "\n" + r'''
import json, sys
if "--version" in sys.argv:
    print("codex-cli 0.159.0"); sys.exit(0)
def send(v):
    print(json.dumps(v), flush=True)
for line in sys.stdin:
    msg = json.loads(line)
    method = msg.get("method")
    if method == "initialized":
        continue
    if method == "initialize":
        send({"id": msg["id"], "result": {}})
    elif method == "thread/start":
        send({"id": msg["id"], "result": {"thread": {"id": "cap-thread"}}})
    elif method == "turn/start":
        send({"id": msg["id"], "result": {"turn": {"id": "cap-turn", "status": "inProgress"}}})
        send({"method": "turn/started", "params": {"threadId": "cap-thread", "turn": {"id": "cap-turn", "status": "inProgress"}}})
        send({
            "method": "error",
            "params": {
                "threadId": "cap-thread",
                "turnId": "cap-turn",
                "willRetry": True,
                "error": {
                    "message": "Selected model is at capacity. Please try a different model.",
                    "codexErrorInfo": {"httpConnectionFailed": {"httpStatusCode": 429}},
                },
            },
        })
        send({
            "method": "turn/completed",
            "params": {
                "threadId": "cap-thread",
                "turn": {
                    "id": "cap-turn",
                    "status": "failed",
                    "error": {
                        "message": "Selected model is at capacity. Please try a different model.",
                        "codexErrorInfo": {"httpConnectionFailed": {"httpStatusCode": 429}},
                    },
                },
            },
        })
''',
        encoding="utf-8",
    )
    server.chmod(0o700)
    _key_path, key = ensure_transport_key(tmp_path)
    directory = tmp_path / "provider-operations" / "aa" / ("e" * 64)
    directory.mkdir(parents=True)
    source = directory / "native-provider-argv.json"
    write_transport_envelope(source, {"argv": [str(server), "exec", "--json", "-"]}, key)

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "meta_research.native_codex_bridge",
            "--provider-argv",
            str(source),
            "--operation-directory",
            str(directory),
        ],
        input="Run experiment.",
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert completed.returncode == 1, completed.stderr
    events = [json.loads(line) for line in completed.stdout.splitlines() if line.strip()]
    error_events = [e for e in events if e.get("type") == "error"]
    assert len(error_events) == 1
    assert "at capacity" in (error_events[0].get("message") or "")
    assert error_events[0].get("classification") == "upstream_capacity"
    assert error_events[0].get("will_retry") is True
    assert events[-1]["type"] == "turn.failed"
    assert events[-1].get("classification") == "upstream_capacity"


def test_s1b_bridge_idle_watchdog_aborts_hung_turn(tmp_path: Path) -> None:
    if os.name != "posix":
        pytest.skip("The production durable provider process boundary runs on Linux")
    server = tmp_path / "hung-codex"
    server.write_text(
        "#!" + sys.executable + "\n" + r'''
import json, sys, time
if "--version" in sys.argv:
    print("codex-cli 0.159.0"); sys.exit(0)
def send(v):
    print(json.dumps(v), flush=True)
for line in sys.stdin:
    msg = json.loads(line)
    method = msg.get("method")
    if method == "initialized":
        continue
    if method == "initialize":
        send({"id": msg["id"], "result": {}})
    elif method == "thread/start":
        send({"id": msg["id"], "result": {"thread": {"id": "hung-thread"}}})
    elif method == "turn/start":
        send({"id": msg["id"], "result": {"turn": {"id": "hung-turn", "status": "inProgress"}}})
        send({"method": "turn/started", "params": {"threadId": "hung-thread", "turn": {"id": "hung-turn", "status": "inProgress"}}})
        time.sleep(60)
''',
        encoding="utf-8",
    )
    server.chmod(0o700)
    _key_path, key = ensure_transport_key(tmp_path)
    directory = tmp_path / "provider-operations" / "bb" / ("f" * 64)
    directory.mkdir(parents=True)
    source = directory / "native-provider-argv.json"
    write_transport_envelope(source, {"argv": [str(server), "exec", "--json", "-"]}, key)

    started = time.monotonic()
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "meta_research.native_codex_bridge",
            "--provider-argv",
            str(source),
            "--operation-directory",
            str(directory),
        ],
        input="Hung turn.",
        text=True,
        capture_output=True,
        env={**os.environ, "META_RESEARCH_NATIVE_TURN_IDLE_TIMEOUT_SECONDS": "0.3"},
        timeout=5,
    )
    elapsed = time.monotonic() - started
    assert elapsed < 4.0
    assert completed.returncode == 75
    assert "native_turn_idle_timeout" in completed.stderr
    events = [json.loads(line) for line in completed.stdout.splitlines() if line.strip()]
    assert any(
        e.get("type") == "error"
        and (e.get("error") or {}).get("code") == "native_turn_idle_timeout"
        for e in events
    )
    assert events[-1]["type"] == "meta_research.native_outcome_unknown"


def test_s1b_bridge_stdout_64mb_cap_protects_oversized_line(tmp_path: Path) -> None:
    if os.name != "posix":
        pytest.skip("The production durable provider process boundary runs on Linux")
    assert getattr(bridge_mod, "MAX_STDOUT_BYTES", None) == 64 * 1024 * 1024
    server = tmp_path / "giant-stdout-codex"
    server.write_text(
        "#!" + sys.executable + "\n" + r'''
import json, sys
if "--version" in sys.argv:
    print("codex-cli 0.159.0"); sys.exit(0)
def send(v):
    print(json.dumps(v), flush=True)
for line in sys.stdin:
    msg = json.loads(line)
    method = msg.get("method")
    if method == "initialized":
        continue
    if method == "initialize":
        send({"id": msg["id"], "result": {}})
    elif method == "thread/start":
        send({"id": msg["id"], "result": {"thread": {"id": "big-thread"}}})
    elif method == "turn/start":
        send({"id": msg["id"], "result": {"turn": {"id": "big-turn", "status": "inProgress"}}})
        send({"method": "turn/started", "params": {"threadId": "big-thread", "turn": {"id": "big-turn", "status": "inProgress"}}})
        # Emit an oversized line exceeding the configured cap
        sys.stdout.write('{"method":"item/commandExecution/outputDelta","params":{"threadId":"big-thread","itemId":"x","delta":"' + ("A" * 16384) + '"}}\n')
        sys.stdout.flush()
''',
        encoding="utf-8",
    )
    server.chmod(0o700)
    _key_path, key = ensure_transport_key(tmp_path)
    directory = tmp_path / "provider-operations" / "cc" / ("1" * 64)
    directory.mkdir(parents=True)
    source = directory / "native-provider-argv.json"
    write_transport_envelope(source, {"argv": [str(server), "exec", "--json", "-"]}, key)

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "meta_research.native_codex_bridge",
            "--provider-argv",
            str(source),
            "--operation-directory",
            str(directory),
        ],
        input="Giant stdout turn.",
        text=True,
        capture_output=True,
        env={**os.environ, "META_RESEARCH_NATIVE_MAX_STDOUT_BYTES": "4096"},
        timeout=5,
    )
    assert completed.returncode == 75
    assert "native_provider_stream_limit" in completed.stderr
    events = [json.loads(line) for line in completed.stdout.splitlines() if line.strip()]
    assert any(
        e.get("type") == "error"
        and (e.get("error") or {}).get("code") == "native_provider_stream_limit"
        for e in events
    )


# ---------------------------------------------------------------------------
# S2a: 60s native delete batch budget + safe file-level logs_2.sqlite rotation
# ---------------------------------------------------------------------------
def test_s2a_native_delete_batch_budget_is_60_seconds() -> None:
    assert cleanup_mod._NATIVE_DELETE_BATCH_SECONDS == 60.0


def _create_wal_logs_db(path: Path, payload_rows: int = 80) -> int:
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("CREATE TABLE IF NOT EXISTS logs (id INTEGER PRIMARY KEY, body TEXT)")
        conn.executemany(
            "INSERT INTO logs (body) VALUES (?)",
            [(f"entry-{i}-" + ("x" * 512),) for i in range(payload_rows)],
        )
        conn.commit()
    finally:
        conn.close()
    return sum(
        p.stat().st_size
        for p in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm"))
        if p.is_file()
    )


def test_s2a_codex_logs_db_safe_rotation_lock_guard_and_bounded_retention(
    tmp_path: Path,
) -> None:
    root = prepare_data_root(tmp_path / "logs-rotate")
    logs_db = root.codex_home / "logs_2.sqlite"
    state_db = root.codex_home / "state_5.sqlite"
    with sqlite3.connect(state_db) as sdb:
        sdb.execute("CREATE TABLE threads (id TEXT PRIMARY KEY)")
        sdb.execute("INSERT INTO threads VALUES ('keep-me')")
        sdb.commit()

    size_before = _create_wal_logs_db(logs_db, payload_rows=80)
    assert size_before > 16384

    # 1. Dry-run reports candidate without mutating logs_2.sqlite
    dry = cleanup_mod.rotate_codex_logs_db(root, dry_run=True, max_bytes=16384, max_archives=2)
    assert len(dry) == 1
    assert dry[0]["action"] == "candidate"
    assert dry[0]["root_kind"] == "codex_logs_db"
    assert dry[0]["bytes"] == size_before
    assert logs_db.is_file()

    # 2. Active SQLite write lock must prevent rotation cleanly
    locker = sqlite3.connect(logs_db, isolation_level=None)
    try:
        locker.execute("BEGIN IMMEDIATE")
        locker.execute("INSERT INTO logs (body) VALUES ('in-flight')")
        locked_report = cleanup_mod.rotate_codex_logs_db(
            root, dry_run=False, max_bytes=16384, max_archives=2
        )
        assert len(locked_report) == 1
        assert locked_report[0]["action"] == "skipped"
        assert locked_report[0]["reason"] in {"codex_logs_db_locked", "codex_logs_db_busy"}
        assert logs_db.is_file()
        locker.execute("COMMIT")
    finally:
        locker.close()

    # 3. Another process holding an open fd (even idle) must prevent rotation on POSIX
    if os.name == "posix":
        holder = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import sqlite3, sys; c = sqlite3.connect(sys.argv[1]); "
                "c.execute('SELECT COUNT(*) FROM logs').fetchone(); "
                "print('ready', flush=True); sys.stdin.read()",
                str(logs_db),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            assert holder.stdout is not None
            assert holder.stdout.readline().strip() == "ready"
            in_use_report = cleanup_mod.rotate_codex_logs_db(
                root, dry_run=False, max_bytes=16384, max_archives=2
            )
            assert len(in_use_report) == 1
            assert in_use_report[0]["action"] == "skipped"
            assert in_use_report[0]["reason"] == "codex_logs_db_in_use"
            assert logs_db.is_file()
        finally:
            if holder.stdin is not None:
                holder.stdin.close()
            holder.wait(timeout=5)

    # 4. Once idle and unlocked, rotation archives cleanly and removes live logs_2.sqlite + WAL sidecars
    rotated = cleanup_mod.rotate_codex_logs_db(root, dry_run=False, max_bytes=16384, max_archives=2)
    assert len(rotated) == 1
    assert rotated[0]["action"] == "rotated"
    assert not logs_db.exists()
    assert not Path(str(logs_db) + "-wal").exists()
    assert not Path(str(logs_db) + "-shm").exists()
    archive_path = Path(rotated[0]["archive_path"])
    assert archive_path.is_file()
    # Decompressed archive must be a valid SQLite DB with all rows including the committed 'in-flight' row
    restored = tmp_path / "restored.sqlite"
    with gzip.open(archive_path, "rb") as src:
        restored.write_bytes(src.read())
    with sqlite3.connect(restored) as rdb:
        count = rdb.execute("SELECT COUNT(*) FROM logs").fetchone()[0]
        assert count == 81

    # 5. Repeated rotations stay bounded to max_archives=2
    for _ in range(3):
        _create_wal_logs_db(logs_db, payload_rows=80)
        res = cleanup_mod.rotate_codex_logs_db(root, dry_run=False, max_bytes=16384, max_archives=2)
        assert res[0]["action"] == "rotated"
    archives = sorted((root.codex_home / "logs-archive").glob("logs_2.*"))
    assert len(archives) == 2

    # 6. state_5.sqlite must remain untouched
    with sqlite3.connect(state_db) as sdb:
        assert sdb.execute("SELECT id FROM threads").fetchall() == [("keep-me",)]


# ---------------------------------------------------------------------------
# D7: prior_target_inputs fixes dead status='completed' -> 'executed',
#     preserves Turn 1 baseline + 3 most recent executed turns in ASC order.
# ---------------------------------------------------------------------------
def test_d7_prior_target_inputs_preserves_gen1_baseline_plus_recent_3_asc(
    tmp_path: Path,
) -> None:
    database, owner, target_request, _handle = _owner_with_active_root(tmp_path / "d7-owner.sqlite3")
    try:
        run = owner.query_run(target_request.request_ref)
        assert run is not None
        with database.write() as conn:
            for gen in range(1, 3):
                conn.execute(
                    text(
                        "INSERT INTO ar_harness_provider_operations "
                        "(operation_ref, run_ref, generation, invocation_hash, status, "
                        "outcome_code, created_at, completed_at, frozen_prompt) "
                        "VALUES (:op, :run, :gen, :ihash, 'executed', NULL, :t, :t, :prompt)"
                    ),
                    {
                        "op": f"{run.run_ref}:harness_turn:{gen}",
                        "run": run.run_ref,
                        "gen": gen,
                        "ihash": canonical_hash({"gen": gen}),
                        "t": float(1000 + gen),
                        "prompt": f"prompt-gen-{gen}",
                    },
                )
        # Fewer than 4 rows: deduplicated chronological ASC order
        assert owner.prior_target_inputs(run.run_ref) == ("prompt-gen-1", "prompt-gen-2")

        with database.write() as conn:
            for gen in range(3, 6):
                conn.execute(
                    text(
                        "INSERT INTO ar_harness_provider_operations "
                        "(operation_ref, run_ref, generation, invocation_hash, status, "
                        "outcome_code, created_at, completed_at, frozen_prompt) "
                        "VALUES (:op, :run, :gen, :ihash, 'executed', NULL, :t, :t, :prompt)"
                    ),
                    {
                        "op": f"{run.run_ref}:harness_turn:{gen}",
                        "run": run.run_ref,
                        "gen": gen,
                        "ihash": canonical_hash({"gen": gen}),
                        "t": float(1000 + gen),
                        "prompt": f"prompt-gen-{gen}",
                    },
                )
            # Failed and running generations must be excluded
            conn.execute(
                text(
                    "INSERT INTO ar_harness_provider_operations "
                    "(operation_ref, run_ref, generation, invocation_hash, status, "
                    "outcome_code, created_at, completed_at, frozen_prompt) "
                    "VALUES (:op, :run, 6, :ihash, 'failed', 'provider_failed', 1006, 1006, 'prompt-failed')"
                ),
                {"op": f"{run.run_ref}:harness_turn:6", "run": run.run_ref, "ihash": canonical_hash({"gen": 6})},
            )
            conn.execute(
                text(
                    "INSERT INTO ar_harness_provider_operations "
                    "(operation_ref, run_ref, generation, invocation_hash, status, "
                    "outcome_code, created_at, completed_at, frozen_prompt) "
                    "VALUES (:op, :run, 7, :ihash, 'running', NULL, 1007, NULL, 'prompt-running')"
                ),
                {"op": f"{run.run_ref}:harness_turn:7", "run": run.run_ref, "ihash": canonical_hash({"gen": 7})},
            )

        prior = owner.prior_target_inputs(run.run_ref)
        # Earliest baseline (gen1) + 3 most recent executed (gen3, gen4, gen5) in strict ASC order
        assert prior == ("prompt-gen-1", "prompt-gen-3", "prompt-gen-4", "prompt-gen-5")
    finally:
        database.close()


def test_d7_target_continuation_uses_lightweight_delta_not_initial_packet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, owner, target_request, _handle = _owner_with_active_root(tmp_path / "d7-e2e.sqlite3")
    try:
        full_conditions = (
            "本次调用的当前运行条件（由系统读取用户配置）：遵循下列最新条件。\n"
            + rc_mod._RESEARCH_TIME_GUIDANCE
            + json.dumps(
                {
                    "quest_ref": "quest-d7",
                    "revision": "rev-1",
                    "text": "INITIAL_HEAVY_POLICY_BLOCK_" + ("Z" * 1500),
                    "research_style": "balanced",
                    "research_time": {
                        "budget_revision": "b1",
                        "approved_seconds": 86400,
                        "used_seconds": 60.0,
                        "remaining_seconds": 86340.0,
                        "overrun_seconds": 0,
                        "observed_at": 1060.0,
                        "accuracy": "exact",
                        "status": "running",
                        "budget_policy": "advisory",
                        "accounting_corrections": [],
                        "unknown_intervals": [],
                    },
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + '\nWork directories: [{"workspace_ref":"ws:1","directory":"/tmp/w1","access":"read_write"}]'
        )
        turn_counter = [0]

        def render_conditions(_workspace, **_scope):
            turn_counter[0] += 1
            if turn_counter[0] == 1:
                return full_conditions
            return full_conditions.replace('"used_seconds":60.0', '"used_seconds":120.0').replace(
                '"observed_at":1060.0', '"observed_at":1120.0'
            )

        monkeypatch.setattr("meta_research.harness.render_runtime_conditions", render_conditions)
        monkeypatch.setattr("meta_research.harness.selected_stage_model", lambda *_a, **_k: target_request.model_ref)
        monkeypatch.setattr(owner, "verify_operation_terminal", lambda _op_ref: None)

        class _GuidanceGateway(_Gateway):
            def issue_channel(self, *args, guidance_binding=None, **kwargs):
                return super().issue_channel(*args, **kwargs)

        adapter = _TargetRootAdapter("codex")
        runtime = HarnessRuntime(owner, _GuidanceGateway(), (adapter,))
        runtime.bind_target_workspace_resolver(_WorkspaceResolver(tmp_path))

        runtime.run_or_resume_target_root(
            target_request.request_ref,
            prompt="Initial Target prompt.",
            mcp_base_url="http://127.0.0.1:8999",
        )
        runtime.run_or_resume_target_root(
            target_request.request_ref,
            prompt="Lightweight continuation prompt.",
            mcp_base_url="http://127.0.0.1:8999",
        )

        assert len(adapter.invocations) == 2
        turn1_conditions, _ = rc_mod.split_runtime_prompt(adapter.invocations[0].prompt)
        turn2_conditions, _ = rc_mod.split_runtime_prompt(adapter.invocations[1].prompt)

        assert "INITIAL_HEAVY_POLICY_BLOCK_" in turn1_conditions
        assert "Work directories:" in turn1_conditions
        assert "INITIAL_HEAVY_POLICY_BLOCK_" not in turn2_conditions
        assert "Work directories:" not in turn2_conditions
        assert "工作目录授权更新=" not in turn2_conditions
        assert len(turn2_conditions) < len(turn1_conditions) // 2
    finally:
        database.close()


# ---------------------------------------------------------------------------
# D8a: Decouple static 361-char _RESEARCH_TIME_GUIDANCE from used_seconds
# ---------------------------------------------------------------------------
def _make_rendered_conditions(*, used_seconds: float, observed_at: float, budget_policy: str = "advisory") -> str:
    payload = {
        "quest_ref": "quest-d8a",
        "revision": "rev-d8a",
        "text": "Keep deterministic conditions.",
        "research_style": "balanced",
        "research_time": {
            "budget_revision": "brev-1",
            "approved_seconds": 86400.0,
            "used_seconds": used_seconds,
            "remaining_seconds": 86400.0 - used_seconds,
            "overrun_seconds": 0.0,
            "observed_at": observed_at,
            "accuracy": "exact",
            "status": "running",
            "budget_policy": budget_policy,
            "accounting_corrections": [],
            "unknown_intervals": [],
        },
    }
    return (
        "本次调用的当前运行条件（由系统读取用户配置）：遵循下列最新条件，并传给委派的智能体。\n"
        + rc_mod._RESEARCH_TIME_GUIDANCE
        + "均衡推进\n"
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    )


def test_d8a_resume_runtime_conditions_decouples_static_time_guidance_from_used_seconds() -> None:
    t1 = _make_rendered_conditions(used_seconds=60.0, observed_at=1060.0)
    t2 = _make_rendered_conditions(used_seconds=120.0, observed_at=1120.0)
    t3 = _make_rendered_conditions(used_seconds=180.0, observed_at=1180.0)
    t4 = _make_rendered_conditions(used_seconds=240.0, observed_at=1240.0)
    t5 = _make_rendered_conditions(used_seconds=300.0, observed_at=1300.0)

    # Initial turn still preserves full guidance
    assert rc_mod._RESEARCH_TIME_GUIDANCE.rstrip() in t1

    # Turn 2 continuation carries updated dynamic timing without repeating 361-char policy text
    delta2 = rc_mod.resume_runtime_conditions(t2, (t1,), ordered=True)
    assert "当前运行条件更新=" in delta2
    parsed2 = json.loads(delta2.splitlines()[0].split("=", 1)[1])
    assert parsed2["research_time"]["used_seconds"] == 120.0
    assert parsed2["research_time"]["observed_at"] == 1120.0
    assert parsed2["research_time"]["budget_policy"] == "advisory"
    assert "1200 秒" not in delta2
    assert "不强制停止、收口或先申请延期" not in delta2

    # Turn 5 with LIMIT 3 window (t1 has aged out of previous) STILL must not re-spam policy text
    delta3 = rc_mod.resume_runtime_conditions(t3, (delta2, t1), ordered=True)
    delta4 = rc_mod.resume_runtime_conditions(t4, (delta3, delta2, t1), ordered=True)
    delta5 = rc_mod.resume_runtime_conditions(t5, (delta4, delta3, delta2), ordered=True)
    assert "1200 秒" not in delta5
    assert "不强制停止、收口或先申请延期" not in delta5
    assert json.loads(delta5.splitlines()[0].split("=", 1)[1])["research_time"]["used_seconds"] == 300.0

    # Legacy session upgrade (previous had no budget_policy) MUST deliver guidance once, then stop
    legacy_prev = 'Historical budget policy: extension first.\n{"research_time":{"approved_seconds":14400}}'
    upgraded = rc_mod.resume_runtime_conditions(t2, (legacy_prev,), ordered=True)
    assert "1200 秒" in upgraded
    assert "新决策使用当前建议预算语义" in upgraded
    after_upgrade = rc_mod.resume_runtime_conditions(t3, (upgraded, legacy_prev), ordered=True)
    assert "1200 秒" not in after_upgrade


# ---------------------------------------------------------------------------
# Round 2 Adversarial Review Regressions (Findings #1 - #6)
# ---------------------------------------------------------------------------
def test_d7_turn5_preserves_baseline_handles_turn1_removals_and_goal_evolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding #1: Turn 5 preserves Turn 1 baseline, emits Turn 1 removals, and tracks G1->G2->G2->G1."""
    from contextlib import nullcontext
    from meta_research.external_mcp import (
        ExternalMcpAccess,
        ExternalMcpOperationSnapshot,
        ExternalMcpServiceConfig,
        FrozenExternalService,
        split_external_mcp_prompt,
    )
    from meta_research.human_guidance import FrozenGuidanceBinding, FrozenGuidanceCut

    database, owner, target_request, _handle = _owner_with_active_root(tmp_path / "d7-turn5.sqlite3")
    try:
        turn_counter = [0]
        goals_by_turn = {1: "G1", 2: "G2", 3: "G2", 4: "G1", 5: "G1"}

        def render_conditions(_workspace, **_scope):
            turn = turn_counter[0]
            payload = {
                "quest_ref": "quest-d7-t5",
                "revision": "rev-1",
                "text": "TURN1_HEAVY_POLICY_BASELINE_" + ("Q" * 1200),
                "research_style": "balanced",
                "research_time": {
                    "budget_revision": "b1",
                    "approved_seconds": 86400,
                    "used_seconds": float(60 * turn),
                    "remaining_seconds": float(86400 - 60 * turn),
                    "overrun_seconds": 0,
                    "observed_at": float(1000 + 60 * turn),
                    "accuracy": "exact",
                    "status": "running",
                    "budget_policy": "advisory",
                    "accounting_corrections": [],
                    "unknown_intervals": [],
                },
            }
            lines = [
                "本次调用的当前运行条件（由系统读取用户配置）：遵循下列最新条件。",
                rc_mod._RESEARCH_TIME_GUIDANCE.rstrip(),
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            ]
            # Turn 5 removes Turn 1's directory and mounted material
            if turn < 5:
                lines.append('Work directories: [{"workspace_ref":"ws:turn1","directory":"/tmp/w1","access":"read_write"}]')
                lines.append(
                    rc_mod._MOUNTED_MATERIALS_MARKER
                    + '[{"reference_ref":"ref:turn1","path":"/tmp/m1.pdf","mode":"read_only"}]'
                )
            else:
                lines.append("Work directories: []")
            return "\n".join(lines)

        class _GuidanceStub:
            def freeze_operation_guidance(self, op):
                turn = turn_counter[0]
                return FrozenGuidanceCut(
                    binding=FrozenGuidanceBinding(
                        identity=op.identity,
                        quest_ref="quest-d7-t5",
                        snapshot_ref=f"snap-{turn}",
                        snapshot_hash=canonical_hash({"snap": turn}),
                    ),
                    provenance_ref="prov-1",
                    deliveries=(),
                    direction_cut={
                        "goal": {"goal_id": goals_by_turn[turn], "statement": f"Goal {goals_by_turn[turn]}"},
                        "conditions": {"style": "balanced"},
                    },
                )

            def human_response_events(self, *, scope):
                return ()

            def prepare_live_guidance(self, *, scope, binding):
                return ()

            def verify_frozen_guidance_binding(self, binding):
                return None

        class _ExternalMcpStub:
            def operation_snapshot(self, *, operation_identity, root_kind, task_prompt, **_kw):
                turn = turn_counter[0]
                if turn < 5:
                    svc = ExternalMcpServiceConfig(
                        service_id="svc-turn1",
                        name="Turn1 Tooling",
                        connection_json=json.dumps({"transport": "http", "url": "http://127.0.0.1:9999"}),
                    )
                    services = (FrozenExternalService(service=svc, server_instructions="inst", catalog_json="[]"),)
                    rev = "mcp-rev-1"
                else:
                    services = ()
                    rev = "mcp-rev-2"
                return ExternalMcpOperationSnapshot(
                    operation_key=operation_identity,
                    root_kind=root_kind,
                    configuration_revision=rev,
                    task_prompt=task_prompt,
                    services=services,
                    snapshot_hash=canonical_hash({"op": operation_identity, "rev": rev}),
                    recovery_guidance="external-mcp-recovery-guidance",
                )

            def channel(self, snapshot, *, resident_token):
                return nullcontext(
                    ExternalMcpAccess(
                        url="http://127.0.0.1:8999/mcp/external",
                        token="ext-tok",
                        binding_json=json.dumps(snapshot.binding()),
                    )
                )

        monkeypatch.setattr("meta_research.harness.render_runtime_conditions", render_conditions)
        monkeypatch.setattr("meta_research.harness.selected_stage_model", lambda *_a, **_k: target_request.model_ref)
        monkeypatch.setattr("meta_research.harness.live_guidance_operation", lambda *_a, **_k: nullcontext())
        monkeypatch.setattr(owner, "verify_operation_terminal", lambda _op_ref: None)

        class _GuidanceGateway(_Gateway):
            def issue_channel(self, *args, guidance_binding=None, **kwargs):
                return super().issue_channel(*args, **kwargs)

        adapter = _TargetRootAdapter("codex")
        runtime = HarnessRuntime(owner, _GuidanceGateway(), (adapter,))
        runtime.bind_target_workspace_resolver(_WorkspaceResolver(tmp_path))
        runtime._human_guidance_authority = _GuidanceStub()
        runtime._external_mcp = _ExternalMcpStub()
        from types import SimpleNamespace
        runtime._resident_scope_verifier = SimpleNamespace(
            verify_root_agent_runtime_scope=lambda **_k: None,
            verify_guidance_read_scope=lambda *_a, **_k: None,
        )

        for turn in range(1, 6):
            turn_counter[0] = turn
            runtime.run_or_resume_target_root(
                target_request.request_ref,
                prompt=f"Turn {turn} prompt.",
                mcp_base_url="http://127.0.0.1:8999",
            )

        assert len(adapter.invocations) == 5
        prompts = [inv.prompt for inv in adapter.invocations]

        # Goal evolution tracking: Turn 1 (G1), Turn 2 (G2 emitted), Turn 3 (G2 NOT re-emitted),
        # Turn 4 (G1 emitted!), Turn 5 (G1 NOT re-emitted).
        assert '"goal_id":"G1"' in prompts[0]
        assert '"goal_id":"G2"' in prompts[1]
        assert "current_goal_and_conditions=" not in prompts[2]
        assert "current_goal_and_conditions=" in prompts[3] and '"goal_id":"G1"' in prompts[3]
        assert "current_goal_and_conditions=" not in prompts[4]

        # Turn 5: baseline fields NOT re-dumped, AND Turn 1 removals ARE emitted!
        t5_prompt = prompts[4]
        assert "TURN1_HEAVY_POLICY_BASELINE_" not in t5_prompt
        assert '"research_style"' not in t5_prompt
        t5_conditions = rc_mod.runtime_conditions_from_prompt(t5_prompt)
        assert "工作目录授权更新=" in t5_conditions
        assert '"removed_workspace_refs":["ws:turn1"]' in t5_conditions
        assert "挂载材料授权更新=" in t5_conditions
        assert '"removed_reference_refs":["ref:turn1"]' in t5_conditions

        # External MCP: Turn 2-4 omitted unchanged svc-turn1; Turn 5 emits removed_service_ids=["svc-turn1"]
        assert split_external_mcp_prompt(prompts[1])[0] == ""
        assert split_external_mcp_prompt(prompts[2])[0] == ""
        assert split_external_mcp_prompt(prompts[3])[0] == ""
        t5_mcp_raw, _ = split_external_mcp_prompt(t5_prompt)
        assert t5_mcp_raw
        t5_mcp = json.loads(t5_mcp_raw)
        assert t5_mcp["removed_service_ids"] == ["svc-turn1"]
        assert t5_mcp["services"] == []
    finally:
        database.close()


def test_s1a_generic_provider_codes_do_not_trigger_upstream_park(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Finding #2 Problem B: Generic catch-all provider/codex codes must NOT trigger upstream parking."""
    for non_upstream in (
        "codex_operation_failed",
        "codex_cli_failed",
        "provider_failed",
        "provider_outcome_unknown",
        "provider_stream_invalid",
        "line 502 error",
        "upstream 400 invalid_request",
    ):
        assert web_mod._is_upstream_capacity_or_transient_error(non_upstream) is False, non_upstream

    recorded_sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        recorded_sleeps.append(delay)
        if len(recorded_sleeps) >= 7:
            raise asyncio.CancelledError()

    monkeypatch.setattr(web_mod.asyncio, "sleep", fake_sleep)
    health = web_mod.ReconciliationHealth()

    async def exercise() -> None:
        with pytest.raises(asyncio.CancelledError):
            await web_mod._process_background_operation(
                lambda: False,
                health=health,
                worker_label="idea stage",
                timeout_code="idea_stage_operation_timeout",
                timeout_seconds=None,
                on_health_change=None,
                transient_error=lambda: "codex_operation_failed",
            )

    asyncio.run(exercise())
    assert health.parked is False
    assert health.warning is None
    assert health.last_error == "codex_operation_failed"
    assert recorded_sleeps[0] == pytest.approx(0.2)
    assert all(d < 30.0 for d in recorded_sleeps[:6])


def test_s1a_unpark_hook_wakes_parked_worker_and_readiness_surfaces_parked_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Finding #2 Problems A & C: Parked workers wake via unpark hook and surface in /internal/readiness."""
    calls = 0
    parked_event = asyncio.Event()
    resumed_event = asyncio.Event()
    fail_flag = [True]

    monkeypatch.setattr(web_mod, "UPSTREAM_FAILURE_BACKOFF_SCHEDULE_SECONDS", (0.005, 0.005))
    monkeypatch.setattr(web_mod, "UPSTREAM_FAILURE_PARK_THRESHOLD", 2)
    monkeypatch.setattr(web_mod, "WORKER_PARK_POLL_SECONDS", 60.0)

    def op() -> bool:
        nonlocal calls
        calls += 1
        if not fail_flag[0]:
            resumed_event.set()
            return True
        return False

    health = web_mod.ReconciliationHealth()

    def on_change() -> None:
        if health.parked:
            parked_event.set()

    async def exercise() -> None:
        task = asyncio.create_task(
            web_mod._process_background_operation(
                op,
                health=health,
                worker_label="idea stage",
                timeout_code="idea_stage_operation_timeout",
                timeout_seconds=None,
                on_health_change=on_change,
                transient_error=lambda: "upstream_capacity" if fail_flag[0] else None,
            )
        )
        try:
            await asyncio.wait_for(parked_event.wait(), timeout=2.0)
            assert health.parked is True
            assert health.warning and "upstream_capacity" in health.warning
            # Unpark via wake hook -> must wake IMMEDIATELY (not wait 60s!)
            fail_flag[0] = False
            started = time.monotonic()
            health.wake()
            await asyncio.wait_for(resumed_event.wait(), timeout=2.0)
            assert (time.monotonic() - started) < 2.0
            assert health.parked is False
            assert health.warning is None
            assert health.status == "ready"
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(exercise())


def test_s1a_internal_readiness_and_web_resume_unpark_workers(tmp_path: Path) -> None:
    """Finding #2 Problems A & C: /internal/readiness reports parked/warning and Web UI resume unparks."""
    from fastapi.testclient import TestClient
    from meta_research.composition import build_production_runtime

    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "readiness-park"),
        startup_harness_diagnostics=False,
    )
    try:
        control_key = "test-control-secret-key"
        app = web_mod.create_app(
            runtime,
            base_url="http://127.0.0.1:8765",
            control_key=control_key,
        )
        headers = {"X-Meta-Research-Control": control_key}
        with TestClient(app, base_url="http://127.0.0.1:8765") as client:
            # Healthy state: readiness MUST NOT include parked/warning keys
            r_ok = client.get("/internal/readiness", headers=headers)
            assert r_ok.status_code == 200
            idea_ok = r_ok.json()["idea_stage"]
            assert "parked" not in idea_ok
            assert "warning" not in idea_ok

            # Park idea_stage_worker -> /internal/readiness MUST expose parked=True and warning
            idea_health = app.state.worker_health["idea_stage_worker"]
            idea_health.status = "unavailable"
            idea_health.last_error = "worker_parked:upstream_capacity"
            idea_health.parked = True
            idea_health.warning = "idea stage parked after 6 consecutive upstream failures"

            r_parked = client.get("/internal/readiness", headers=headers)
            assert r_parked.status_code == 200
            idea_parked = r_parked.json()["idea_stage"]
            assert idea_parked["parked"] is True
            assert "parked" in idea_parked["warning"]

            # Invoking unpark_workers() clears parked/warning cleanly
            unparked = app.state.unpark_workers()
            assert "idea_stage_worker" in unparked
            assert idea_health.parked is False
            assert idea_health.warning is None
            r_after = client.get("/internal/readiness", headers=headers)
            assert "parked" not in r_after.json()["idea_stage"]

            # Re-park and verify Web UI resume command execution endpoint triggers unpark_workers automatically!
            idea_health.status = "unavailable"
            idea_health.last_error = "worker_parked:upstream_capacity"
            idea_health.parked = True
            idea_health.warning = "idea stage parked again"

            orig_exec = runtime.owners.human_collaboration.execute_confirmed_command
            runtime.owners.human_collaboration.execute_confirmed_command = (
                lambda intent_id, receipt_ref, idem: {
                    "intent_id": intent_id,
                    "draft": {"command_kind": "research_control", "payload": {"action": "resume"}},
                }
            )
            try:
                boot = client.post("/internal/bootstrap-token", headers=headers).json()["bootstrap_token"]
                auth_resp = client.post(
                    "/auth/bootstrap",
                    headers={"Origin": "http://127.0.0.1:8765"},
                    json={"token": boot},
                )
                assert auth_resp.status_code == 200
                csrf = auth_resp.json()["csrf_token"]
                assert csrf
                exec_resp = client.post(
                    "/api/v1/human-collaboration/commands/intent-resume-1/executions",
                    json={"confirmation_receipt_ref": "rcpt-1"},
                    headers={
                        "Origin": "http://127.0.0.1:8765",
                        "X-CSRF-Token": csrf,
                        "Idempotency-Key": "idem-unpark-1",
                    },
                )
                assert exec_resp.status_code == 201
                assert idea_health.parked is False
                assert idea_health.warning is None
                r_resumed = client.get("/internal/readiness", headers=headers)
                assert "parked" not in r_resumed.json()["idea_stage"]
            finally:
                runtime.owners.human_collaboration.execute_confirmed_command = orig_exec
    finally:
        runtime.close()


def test_s1a_s1b_e2e_stage_and_target_upstream_capacity_flows_to_backoff_and_park(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding #3 Problems A, B, C: S1b capacity/timeout flows through adapters/workers into S1a backoff & park."""
    from types import SimpleNamespace
    from meta_research.harness import HarnessAdmissionError
    from meta_research.harness_adapters import CodexHarnessAdapter, HarnessAdapterUnavailable, HarnessInvocation
    from meta_research.sealed_provider_operations import ProviderOperationUnavailable
    from meta_research.target_run_runtime import TargetRunRuntimeStatus

    # Part 1: S1b stdout events -> CodexHarnessAdapter -> HarnessAdapterUnavailable carries upstream_classification
    capacity_stdout = (
        json.dumps({"type": "thread.started", "thread_id": "t-cap"}) + "\n"
        + json.dumps({
            "type": "error",
            "thread_id": "t-cap",
            "turn_id": "turn-1",
            "message": "Selected model is at capacity.",
            "error": {"message": "Selected model is at capacity.", "codexErrorInfo": {"httpConnectionFailed": {"httpStatusCode": 429}}},
            "will_retry": True,
            "classification": "upstream_capacity",
        }) + "\n"
        + json.dumps({
            "type": "turn.failed",
            "turn_id": "turn-1",
            "error": {"message": "Selected model is at capacity.", "codexErrorInfo": {"httpConnectionFailed": {"httpStatusCode": 429}}},
            "classification": "upstream_capacity",
        }) + "\n"
    )
    idle_stdout = (
        json.dumps({"type": "thread.started", "thread_id": "t-idle"}) + "\n"
        + json.dumps({
            "type": "error",
            "thread_id": "t-idle",
            "turn_id": "turn-2",
            "message": "Native turn idle timeout after 900.0s without activity",
            "error": {"code": "native_turn_idle_timeout", "idle_timeout_seconds": 900.0},
            "will_retry": False,
            "classification": "upstream_transient",
        }) + "\n"
        + json.dumps({"type": "meta_research.native_outcome_unknown"}) + "\n"
    )

    def _make_invocation() -> HarnessInvocation:
        return HarnessInvocation(
            harness_family="codex",
            provider_operation_ref="op-cap-1",
            run_ref="run-cap-1",
            attempt_ref="att-1",
            attempt_generation=1,
            root_session_ref="rs-1",
            fence_ref="fence-1",
            model_ref="gpt-5.4",
            prompt="hello",
            mcp_url="http://127.0.0.1:8999/mcp",
            mcp_token="tok",
            root_kind="target",
        )

    adapter_cap = CodexHarnessAdapter(
        tmp_path,
        runner=lambda *_a, **_k: subprocess.CompletedProcess(["codex"], 1, capacity_stdout, ""),
    )
    monkeypatch.setattr(adapter_cap, "_provider_version", lambda _env=None: "0.159.0")
    monkeypatch.setattr(adapter_cap, "_provider_feature_inventory", lambda *_a, **_k: {})
    with pytest.raises(HarnessAdapterUnavailable) as cap_exc:
        adapter_cap.invoke(_make_invocation())
    assert cap_exc.value.code == "provider_failed"
    assert getattr(cap_exc.value, "upstream_classification", None) == "upstream_capacity"

    adapter_idle = CodexHarnessAdapter(
        tmp_path,
        runner=lambda *_a, **_k: subprocess.CompletedProcess(["codex"], 75, idle_stdout, ""),
    )
    monkeypatch.setattr(adapter_idle, "_provider_version", lambda _env=None: "0.159.0")
    monkeypatch.setattr(adapter_idle, "_provider_feature_inventory", lambda *_a, **_k: {})
    with pytest.raises(HarnessAdapterUnavailable) as idle_exc:
        adapter_idle.invoke(_make_invocation())
    assert idle_exc.value.code == "provider_outcome_unknown"
    assert getattr(idle_exc.value, "upstream_classification", None) == "native_turn_idle_timeout"

    # Part 2: Stage path -> ProviderOperationUnavailable(code="codex_operation_failed", upstream_classification="upstream_capacity")
    # triggers exact 30->60->120->240->480s backoff and parking in _process_background_operation!
    orig_asyncio_sleep = asyncio.sleep
    stage_err = ProviderOperationUnavailable("codex_operation_failed", upstream_classification="upstream_capacity")
    assert stage_err.code == "codex_operation_failed"
    assert stage_err.upstream_classification == "upstream_capacity"
    stage_sleeps: list[float] = []
    stage_health = web_mod.ReconciliationHealth()

    async def fake_stage_sleep(delay: float) -> None:
        stage_sleeps.append(delay)
        if stage_health.parked or len(stage_sleeps) >= 8:
            raise asyncio.CancelledError()

    monkeypatch.setattr(web_mod.asyncio, "sleep", fake_stage_sleep)

    async def run_stage() -> None:
        with pytest.raises(asyncio.CancelledError):
            await web_mod._process_background_operation(
                lambda: False,
                health=stage_health,
                worker_label="bundle stage",
                timeout_code="bundle_stage_operation_timeout",
                timeout_seconds=None,
                on_health_change=None,
                transient_error=lambda: stage_err.code,
                transient_error_classification=lambda: stage_err.upstream_classification,
            )

    asyncio.run(run_stage())
    assert stage_sleeps[:5] == [30.0, 60.0, 120.0, 240.0, 480.0]
    assert stage_health.parked is True
    assert stage_health.last_error == "worker_parked:codex_operation_failed"

    # Part 3: Target path -> TargetRunRuntimeStatus with upstream_classification flows into _process_target_runs
    # backoff (30->60->120->240->480s) and parks on 6th consecutive failure!
    monkeypatch.setattr(web_mod.asyncio, "sleep", orig_asyncio_sleep)
    fake_now = [1000.0]
    recorded_due_deltas: list[float] = []
    target_calls = 0
    target_health = web_mod.ReconciliationHealth()

    class _FakeTargetRuntime:
        def has_pending_cancel(self, _ref: str) -> bool:
            return False

        def process_once(self, _ref: str) -> bool:
            nonlocal target_calls
            target_calls += 1
            return False

        def query_status(self, target_ref: str) -> TargetRunRuntimeStatus:
            return TargetRunRuntimeStatus(
                target_ref=target_ref,
                phase="root_pending",
                pending_code="provider_failed",
                upstream_classification="upstream_capacity",
            )

    class _FakeInventory:
        def list_target_root_work_refs(self):
            return ("target-cap-1",)

        def list_target_root_reconciliation_refs(self):
            return ()

    target_runtime = SimpleNamespace(
        owners=SimpleNamespace(agent_runtime=_FakeInventory()),
        target_run_runtime=_FakeTargetRuntime(),
    )

    async def run_target() -> None:
        loop = asyncio.get_running_loop()
        monkeypatch.setattr(loop, "time", lambda: fake_now[0])

        async def fast_sleep(delay: float) -> None:
            if target_health.parked:
                raise asyncio.CancelledError()
            # When the target is backed off via next_retry_at, record delay & jump clock forward
            if target_health.next_retry_at is not None and len(recorded_due_deltas) < target_calls:
                recorded_due_deltas.append(round(target_health.next_retry_at - time.time()))
                fake_now[0] += 1000.0
            await orig_asyncio_sleep(0)

        monkeypatch.setattr(web_mod.asyncio, "sleep", fast_sleep)
        with pytest.raises(asyncio.CancelledError):
            await web_mod._process_target_runs(target_runtime, target_health)

    asyncio.run(run_target())
    assert target_calls == 6
    assert target_health.parked is True
    assert target_health.last_error == "worker_parked:provider_failed"
    assert len(recorded_due_deltas) == 5
    for observed, expected in zip(recorded_due_deltas, (30, 60, 120, 240, 480), strict=True):
        assert abs(observed - expected) <= 2


def test_s1a_target_runs_discovery_error_backoff_escalates_on_identical_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Finding #3 Problem D: Repeated identical discovery errors in _process_target_runs must escalate backoff."""
    from types import SimpleNamespace

    recorded_sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        recorded_sleeps.append(delay)
        if len(recorded_sleeps) >= 5:
            raise asyncio.CancelledError()

    monkeypatch.setattr(web_mod.asyncio, "sleep", fake_sleep)

    class _FailingInventory:
        def list_target_root_work_refs(self):
            raise OwnerConflict("target_inventory_locked")

        def list_target_root_reconciliation_refs(self):
            return ()

    runtime = SimpleNamespace(
        owners=SimpleNamespace(agent_runtime=_FailingInventory()),
        target_run_runtime=SimpleNamespace(),
    )
    health = web_mod.ReconciliationHealth()

    async def exercise() -> None:
        with pytest.raises(asyncio.CancelledError):
            await web_mod._process_target_runs(runtime, health)

    asyncio.run(exercise())
    assert recorded_sleeps == pytest.approx([0.2, 0.4, 0.8, 1.6, 3.2])


def test_s1a_s2a_verify_research_assets_cleanup_decoupled_from_5s_watchdog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Finding #4: Slow cleanup (>5s asset watchdog) must NOT trip asset_verification_io_timeout or thrash."""
    from types import SimpleNamespace

    monkeypatch.setattr(web_mod, "ASSET_WORKER_WATCHDOG_SECONDS", 0.08)
    monkeypatch.setattr(web_mod, "ASSET_CLEANUP_WATCHDOG_SECONDS", 1.5, raising=False)
    monkeypatch.setattr(web_mod, "ASSET_CLEANUP_INTERVAL_SECONDS", 0.25, raising=False)

    cleanup_windows: list[tuple[float, float]] = []
    seen_errors: list[str | None] = []
    done = asyncio.Event()

    def verify_once() -> bool:
        time.sleep(0.005)
        return False

    def slow_cleanup(*, dry_run: bool = False, limit: int = 10):
        start = time.monotonic()
        time.sleep(0.18)  # > 0.08s asset watchdog, < 1.5s cleanup watchdog
        end = time.monotonic()
        cleanup_windows.append((start, end))
        if len(cleanup_windows) >= 2:
            done.set()
        return ()

    runtime = SimpleNamespace(
        owners=SimpleNamespace(
            research_memory=SimpleNamespace(verify_asset_inventory_once=verify_once)
        ),
        target_run_runtime=SimpleNamespace(cleanup_completed_workspaces=slow_cleanup),
    )
    health = web_mod.ReconciliationHealth()

    def on_change() -> None:
        seen_errors.append(health.last_error)

    async def exercise() -> None:
        task = asyncio.create_task(web_mod._verify_research_assets(runtime, health, on_change))
        try:
            await asyncio.wait_for(done.wait(), timeout=3.0)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(exercise())
    assert "asset_verification_io_timeout" not in seen_errors
    assert health.status == "ready"
    assert len(cleanup_windows) >= 2
    gap_after_first_finish = cleanup_windows[1][0] - cleanup_windows[0][1]
    assert gap_after_first_finish >= 0.20, f"expected cooldown >=0.25s after finish, got {gap_after_first_finish:.3f}s"


def test_s2a_logs_db_rotation_safety_guards_and_expanded_active_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding #5: Verify journal_mode check, no post-replace sidecar unlink, orphan tmp cleanup, and expanded active DB check."""
    # A. If WAL sidecar persists after close, abort BEFORE os.replace!
    root = prepare_data_root(tmp_path / "s2a-guards")
    logs_db = root.codex_home / "logs_2.sqlite"
    wal_path = root.codex_home / "logs_2.sqlite-wal"
    shm_path = root.codex_home / "logs_2.sqlite-shm"
    _create_wal_logs_db(logs_db, payload_rows=80)

    real_connect = cleanup_mod.sqlite3.connect

    class _FakeWalCursor:
        def __init__(self, row):
            self._row = row

        def fetchone(self):
            return self._row

    class _StubbornWalConn:
        def __init__(self, inner):
            self._inner = inner

        def execute(self, sql, *args, **kwargs):
            if "journal_mode = DELETE" in sql:
                # Simulate SQLite silently refusing to leave WAL mode
                return _FakeWalCursor(("wal",))
            return self._inner.execute(sql, *args, **kwargs)

        def close(self):
            return self._inner.close()

    monkeypatch.setattr(cleanup_mod.sqlite3, "connect", lambda *a, **kw: _StubbornWalConn(real_connect(*a, **kw)))
    res_wal_stuck = cleanup_mod.rotate_codex_logs_db(root, dry_run=False, max_bytes=16384, max_archives=2)
    assert res_wal_stuck[0]["action"] == "skipped"
    assert res_wal_stuck[0]["reason"] == "codex_logs_db_busy"
    assert logs_db.is_file()
    monkeypatch.setattr(cleanup_mod.sqlite3, "connect", real_connect)

    # B. Never unlink -wal/-shm created by a concurrent Codex process AFTER os.replace!
    archive_dir = root.codex_home / "logs-archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    orphan_tmp = archive_dir / ".logs_2.20260101T000000Z-000000001.sqlite.gz.tmp"
    orphan_tmp.write_bytes(b"stale-partial-gzip")
    real_replace = cleanup_mod.os.replace

    def racing_replace(src, dst):
        real_replace(src, dst)
        if Path(src) == logs_db:
            # Simulate a brand-new Codex process starting right after os.replace
            logs_db.write_bytes(b"new-db")
            wal_path.write_bytes(b"new-live-wal")
            shm_path.write_bytes(b"new-live-shm")

    monkeypatch.setattr(cleanup_mod.os, "replace", racing_replace)
    res_race = cleanup_mod.rotate_codex_logs_db(root, dry_run=False, max_bytes=16384, max_archives=2)
    monkeypatch.setattr(cleanup_mod.os, "replace", real_replace)
    assert res_race[0]["action"] == "rotated"
    # Brand-new live sidecars MUST NOT have been deleted!
    assert wal_path.read_bytes() == b"new-live-wal"
    assert shm_path.read_bytes() == b"new-live-shm"
    # Stale orphan tmp file MUST have been cleaned up!
    assert not orphan_tmp.exists()

    # C. Expanded active-Codex Owner DB & research-recorder checks block rotation cleanly
    from meta_research.database import Database
    from meta_research.migration import upgrade_database
    db_path = tmp_path / "s2a-active.sqlite3"
    upgrade_database(db_path)
    database = Database(db_path)
    try:
        root2 = prepare_data_root(tmp_path / "s2a-active-root")
        logs_db2 = root2.codex_home / "logs_2.sqlite"
        _create_wal_logs_db(logs_db2, payload_rows=80)
        monkeypatch.setattr(cleanup_mod, "_CODEX_LOGS_DB_MAX_BYTES", 16384)
        cleaner = cleanup_mod.NativeSessionCleanup(root2, "codex", None)

        # 1) ar_run_controls status='running' blocks rotation
        with database.write() as conn:
            conn.execute(text(
                "INSERT INTO ar_run_controls (run_ref, run_kind, status, control_revision, cleanup_status, updated_at) "
                "VALUES ('ctrl-1', 'deepfetch', 'running', 1, 'none', 1000.0)"
            ))
        blocked_ctrl = cleaner.rotate_logs_db(database, dry_run=False)
        assert blocked_ctrl[0]["action"] == "skipped"
        assert blocked_ctrl[0]["reason"] == "codex_logs_db_in_use"
        with database.write() as conn:
            conn.execute(text("UPDATE ar_run_controls SET status = 'completed' WHERE run_ref = 'ctrl-1'"))

        # 2) research-recorder/summaries.sqlite3 active job blocks rotation
        recorder_dir = root2.root / "research-recorder"
        recorder_dir.mkdir(parents=True, exist_ok=True)
        rec_db = recorder_dir / "summaries.sqlite3"
        with sqlite3.connect(rec_db) as rconn:
            rconn.execute("CREATE TABLE jobs (job_id TEXT PRIMARY KEY, status TEXT)")
            rconn.execute("INSERT INTO jobs VALUES ('j1', 'active')")
            rconn.commit()
        blocked_rec = cleaner.rotate_logs_db(database, dry_run=False)
        assert blocked_rec[0]["action"] == "skipped"
        assert blocked_rec[0]["reason"] == "codex_logs_db_in_use"
        with sqlite3.connect(rec_db) as rconn:
            rconn.execute("UPDATE jobs SET status = 'completed' WHERE job_id = 'j1'")
            rconn.commit()

        # 3) Once all active Codex sources finish, rotation succeeds!
        ok_rot = cleaner.rotate_logs_db(database, dry_run=False)
        assert ok_rot[0]["action"] == "rotated"
    finally:
        database.close()


def test_s1b_active_tool_exempt_from_short_idle_timeout_and_sends_turn_interrupt(
    tmp_path: Path,
) -> None:
    """Finding #6 Problem A: In-flight commandExecution uses active-tool timeout; abort sends turn/interrupt."""
    if os.name != "posix":
        pytest.skip("The production durable provider process boundary runs on Linux")

    # Part 1: Silent 0.55s commandExecution succeeds when turn_idle_timeout=0.25s < 0.55s < active_tool=2.0s
    server_ok = tmp_path / "active-tool-ok-codex"
    server_ok.write_text(
        "#!" + sys.executable + "\n" + r'''
import json, sys, time
if "--version" in sys.argv:
    print("codex-cli 0.159.0"); sys.exit(0)
def send(v):
    print(json.dumps(v), flush=True)
for line in sys.stdin:
    msg = json.loads(line)
    m = msg.get("method")
    if m == "initialized":
        continue
    if m == "initialize":
        send({"id": msg["id"], "result": {}})
    elif m == "thread/start":
        send({"id": msg["id"], "result": {"thread": {"id": "tool-thread"}}})
    elif m == "turn/start":
        send({"id": msg["id"], "result": {"turn": {"id": "tool-turn", "status": "inProgress"}}})
        send({"method": "turn/started", "params": {"threadId": "tool-thread", "turn": {"id": "tool-turn", "status": "inProgress"}}})
        send({"method": "item/started", "params": {"threadId": "tool-thread", "turnId": "tool-turn", "item": {"type": "commandExecution", "id": "cmd-1", "command": "python train.py"}}})
        time.sleep(0.55)
        send({"method": "item/completed", "params": {"threadId": "tool-thread", "turnId": "tool-turn", "item": {"type": "commandExecution", "id": "cmd-1", "exitCode": 0}}})
        send({"method": "item/completed", "params": {"threadId": "tool-thread", "turnId": "tool-turn", "item": {"type": "agentMessage", "text": "{\"ok\":true}"}}})
        send({"method": "turn/completed", "params": {"threadId": "tool-thread", "turn": {"id": "tool-turn", "status": "completed"}}})
''',
        encoding="utf-8",
    )
    server_ok.chmod(0o700)
    _key_path, key = ensure_transport_key(tmp_path)
    dir1 = tmp_path / "provider-operations" / "t1" / ("a" * 64)
    dir1.mkdir(parents=True)
    src1 = dir1 / "native-provider-argv.json"
    write_transport_envelope(src1, {"argv": [str(server_ok), "exec", "--json", "-"]}, key)

    res_ok = subprocess.run(
        [sys.executable, "-m", "meta_research.native_codex_bridge", "--provider-argv", str(src1), "--operation-directory", str(dir1)],
        input="Run GPU training.",
        text=True,
        capture_output=True,
        env={
            **os.environ,
            "META_RESEARCH_NATIVE_TURN_IDLE_TIMEOUT_SECONDS": "0.25",
            "META_RESEARCH_NATIVE_ACTIVE_TOOL_IDLE_TIMEOUT_SECONDS": "3.0",
        },
        timeout=8,
    )
    assert res_ok.returncode == 0, res_ok.stderr

    # Part 2: Watchdog sends turn/interrupt before SIGTERM; honoring it flushes terminal turn.failed cleanly
    interrupt_flag = tmp_path / "interrupt-seen.txt"
    server_int = tmp_path / "interrupt-codex"
    server_int.write_text(
        "#!" + sys.executable + "\n" + f'''
import json, pathlib, sys
if "--version" in sys.argv:
    print("codex-cli 0.159.0"); sys.exit(0)
def send(v):
    print(json.dumps(v), flush=True)
for line in sys.stdin:
    msg = json.loads(line)
    m = msg.get("method")
    if m == "initialized":
        continue
    if m == "initialize":
        send({{"id": msg["id"], "result": {{}}}})
    elif m == "thread/start":
        send({{"id": msg["id"], "result": {{"thread": {{"id": "int-thread"}}}}}})
    elif m == "turn/start":
        send({{"id": msg["id"], "result": {{"turn": {{"id": "int-turn", "status": "inProgress"}}}}}})
        send({{"method": "turn/started", "params": {{"threadId": "int-thread", "turn": {{"id": "int-turn", "status": "inProgress"}}}}}})
    elif m == "turn/interrupt":
        pathlib.Path({str(interrupt_flag)!r}).write_text(json.dumps(msg.get("params")), encoding="utf-8")
        send({{"id": msg["id"], "result": {{}}}})
        send({{"method": "turn/completed", "params": {{"threadId": "int-thread", "turn": {{"id": "int-turn", "status": "interrupted"}}}}}})
''',
        encoding="utf-8",
    )
    server_int.chmod(0o700)
    dir2 = tmp_path / "provider-operations" / "t2" / ("b" * 64)
    dir2.mkdir(parents=True)
    src2 = dir2 / "native-provider-argv.json"
    write_transport_envelope(src2, {"argv": [str(server_int), "exec", "--json", "-"]}, key)

    res_int = subprocess.run(
        [sys.executable, "-m", "meta_research.native_codex_bridge", "--provider-argv", str(src2), "--operation-directory", str(dir2)],
        input="Stalled turn.",
        text=True,
        capture_output=True,
        env={**os.environ, "META_RESEARCH_NATIVE_TURN_IDLE_TIMEOUT_SECONDS": "0.25"},
        timeout=8,
    )
    assert interrupt_flag.is_file(), "Bridge watchdog must send turn/interrupt before SIGTERM"
    assert res_int.returncode == 1, res_int.stderr
    events = [json.loads(line) for line in res_int.stdout.splitlines() if line.strip()]
    assert events[-1]["type"] == "turn.failed"
    assert events[-1].get("classification") == "upstream_transient"


def test_s1b_last_error_classification_clears_after_progress_and_regex_rejects_false_positives(
    tmp_path: Path,
) -> None:
    """Finding #6 Problems B & C: Progress clears stale error classification; loose 502/upstream strings rejected."""
    assert bridge_mod.classify_native_error({"message": "line 502 error"}) is None
    assert bridge_mod.classify_native_error({"message": "upstream 400 invalid_request"}) is None
    assert bridge_mod.classify_native_error({"message": "HTTP 502 Bad Gateway"}) == "upstream_transient"
    assert bridge_mod.classify_native_error({"message": "status 429 too many requests"}) == "upstream_capacity"

    if os.name != "posix":
        pytest.skip("The production durable provider process boundary runs on Linux")

    server = tmp_path / "recover-then-403-codex"
    server.write_text(
        "#!" + sys.executable + "\n" + r'''
import json, sys
if "--version" in sys.argv:
    print("codex-cli 0.159.0"); sys.exit(0)
def send(v):
    print(json.dumps(v), flush=True)
for line in sys.stdin:
    msg = json.loads(line)
    m = msg.get("method")
    if m == "initialized":
        continue
    if m == "initialize":
        send({"id": msg["id"], "result": {}})
    elif m == "thread/start":
        send({"id": msg["id"], "result": {"thread": {"id": "clr-thread"}}})
    elif m == "turn/start":
        send({"id": msg["id"], "result": {"turn": {"id": "clr-turn", "status": "inProgress"}}})
        send({"method": "turn/started", "params": {"threadId": "clr-thread", "turn": {"id": "clr-turn", "status": "inProgress"}}})
        # 1. Transient capacity blip
        send({
            "method": "error",
            "params": {
                "threadId": "clr-thread",
                "turnId": "clr-turn",
                "willRetry": True,
                "error": {"message": "status 429 too many requests", "codexErrorInfo": {"httpConnectionFailed": {"httpStatusCode": 429}}},
            },
        })
        # 2. Normal progress recovers from the blip
        send({"method": "item/started", "params": {"threadId": "clr-thread", "turnId": "clr-turn", "item": {"type": "agentMessage", "id": "m1"}}})
        send({"method": "item/completed", "params": {"threadId": "clr-thread", "turnId": "clr-turn", "item": {"type": "agentMessage", "id": "m1", "text": "working"}}})
        # 3. Later permanent 403 error terminates the turn
        send({
            "method": "turn/completed",
            "params": {
                "threadId": "clr-thread",
                "turn": {
                    "id": "clr-turn",
                    "status": "failed",
                    "error": {
                        "message": "unexpected status 403 Forbidden: insufficient balance",
                        "codexErrorInfo": {"httpConnectionFailed": {"httpStatusCode": 403}},
                    },
                },
            },
        })
''',
        encoding="utf-8",
    )
    server.chmod(0o700)
    _key_path, key = ensure_transport_key(tmp_path)
    directory = tmp_path / "provider-operations" / "t3" / ("c" * 64)
    directory.mkdir(parents=True)
    source = directory / "native-provider-argv.json"
    write_transport_envelope(source, {"argv": [str(server), "exec", "--json", "-"]}, key)

    res = subprocess.run(
        [sys.executable, "-m", "meta_research.native_codex_bridge", "--provider-argv", str(source), "--operation-directory", str(directory)],
        input="Recover then fail 403.",
        text=True,
        capture_output=True,
        timeout=8,
    )
    assert res.returncode == 1, res.stderr
    events = [json.loads(line) for line in res.stdout.splitlines() if line.strip()]
    assert events[-1]["type"] == "turn.failed"
    assert "classification" not in events[-1], f"Stale classification leaked into 403 failure: {events[-1]}"
