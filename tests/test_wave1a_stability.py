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
# D7: prior_target_inputs fixes dead status='completed' -> 'executed' + LIMIT 3
# ---------------------------------------------------------------------------
def test_d7_prior_target_inputs_returns_executed_prompts_with_limit_3_desc(
    tmp_path: Path,
) -> None:
    database, owner, target_request, _handle = _owner_with_active_root(tmp_path / "d7-owner.sqlite3")
    try:
        run = owner.query_run(target_request.request_ref)
        assert run is not None
        with database.write() as conn:
            for gen in range(1, 6):
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
        assert prior == ("prompt-gen-5", "prompt-gen-4", "prompt-gen-3")
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

        adapter = _TargetRootAdapter("codex")
        runtime = HarnessRuntime(owner, _Gateway(), (adapter,))
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
