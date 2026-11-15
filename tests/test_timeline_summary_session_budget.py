import sqlite3

from meta_research.timeline_summaries import TimelineSummaryStore


def source(version, key="stage:c1:bundle"):
    return {"node_key": key, "kind": "stage", "source_hash": str(version),
            "sources": [{"ref": "source-1"}], "reader": {"source_ref": "source-1"}}


def publish(store, job, native, now):
    store.publish(job, [{"node_key": n["node_key"], "source_hash": n["source_hash"],
        "summary": "Analysis continues; independent validation is still needed.", "source_refs": ["source-1"]}
        for n in job["nodes"]], native, now)


def test_recorder_rotates_native_after_every_three_completed_calls_across_restart(tmp_path):
    path = tmp_path / "summary.sqlite3"
    for i in range(8):
        store = TimelineSummaryStore(path)
        store.sync("quest-1", [source(i)], i * 400)
        job = store.claim(i * 400, "zh")
        native = f"native-recorder-{i // 3}"
        assert job["native_session_ref"] == (None if i % 3 == 0 else native)
        assert "content" not in job["nodes"][0]
        publish(store, job, native, i * 400 + 1)
        publish(store, job, native, i * 400 + 2)  # Duplicate settlement consumes no call.
    assert store.query("quest-1")["nodes"][0]["status"] == "ready"


def test_terminal_failure_consumes_call_and_retries_basis_under_new_operation(tmp_path):
    path = tmp_path / "summary.sqlite3"
    store = TimelineSummaryStore(path)
    for i in range(2):
        store.sync("quest-1", [source(i)], i * 400)
        publish(store, store.claim(i * 400, "zh"), "native-recorder", i * 400 + 1)
    store.sync("quest-1", [source("original")], 800)
    job = store.claim(800, "zh")
    store.fail(job, "codex_cli_failed", 801, "native-recorder", terminal=True)
    store.fail(job, "late_duplicate_failure", 802, "native-recorder", terminal=True)
    assert TimelineSummaryStore(path).claim(1100, "zh") is None
    retried = TimelineSummaryStore(path).claim(1101, "en")
    assert retried["job_ref"] != job["job_ref"]
    assert retried["nodes"] == job["nodes"]
    assert retried["observed_at"] == job["observed_at"]
    assert retried["output_language"] == "zh"
    assert retried["native_session_ref"] is None


def test_unstarted_failure_does_not_consume_session_call(tmp_path):
    store = TimelineSummaryStore(tmp_path / "summary.sqlite3")
    for i in range(2):
        store.sync("quest-1", [source(i)], i * 400)
        publish(store, store.claim(i * 400, "zh"), "native-recorder", i * 400 + 1)
    store.sync("quest-1", [source("original")], 800)
    job = store.claim(800, "zh")
    store.fail(job, "timeline_summary_skill_unavailable", 801)
    retry = store.claim(1101, "en")
    assert retry["job_ref"] == job["job_ref"]
    assert retry["native_session_ref"] == "native-recorder"
    publish(store, retry, "native-recorder", 1102)
    store.sync("quest-1", [source("next")], 1200)
    assert store.claim(1200, "zh")["native_session_ref"] is None


def test_queued_corrections_select_session_when_claimed_not_when_enqueued(tmp_path):
    store = TimelineSummaryStore(tmp_path / "summary.sqlite3")
    store.sync("quest-1", [source(0)], 0)
    publish(store, store.claim(0, "zh"), "native-recorder", 1)
    nodes = [source(1, f"stage:c{i}:plan") for i in range(2, 5)]
    store.sync("quest-1", nodes, 400)
    batch = store.claim(400, "zh")
    feedback = {n["node_key"]: {"code": "unallowed_source_refs", "source_refs": ["source-1"],
        "message": "Use a source authorized for this node."} for n in nodes}
    store.publish(batch, [], "native-recorder", 401, failures=feedback)
    first = store.claim(402, "zh")
    assert first["native_session_ref"] == "native-recorder"
    publish(store, first, "native-recorder", 403)
    second = TimelineSummaryStore(store.path).claim(404, "zh")
    assert second["native_session_ref"] is None
    publish(store, second, "new-native", 405)
    third = store.claim(406, "zh")
    assert third["native_session_ref"] == "new-native"


def test_legacy_native_is_rotated_but_active_request_is_kept_for_recovery(tmp_path):
    store = TimelineSummaryStore(tmp_path / "summary.sqlite3")
    store.sync("quest-1", [source(0)], 0)
    publish(store, store.claim(0, "zh"), "legacy-native", 1)
    store.sync("quest-1", [source(1)], 400)
    active = store.claim(400, "zh")
    with sqlite3.connect(store.path) as db:
        db.execute("ALTER TABLE quests DROP COLUMN session_call_count")
    restarted = TimelineSummaryStore(store.path)
    recovered = restarted.claim(401, "zh")
    assert recovered["job_ref"] == active["job_ref"]
    assert recovered["native_session_ref"] == "legacy-native"
    assert recovered["nodes"] == active["nodes"]
    publish(restarted, recovered, "legacy-native", 402)
    restarted.sync("quest-1", [source(2)], 800)
    assert restarted.claim(800, "zh")["native_session_ref"] is None


def test_legacy_failed_spool_request_changes_only_after_absence_is_confirmed(tmp_path):
    store = TimelineSummaryStore(tmp_path / "summary.sqlite3")
    store.sync("quest-1", [source(0)], 0)
    publish(store, store.claim(0, "zh"), "legacy-native", 1)
    store.sync("quest-1", [source(1)], 400)
    job = store.claim(400, "zh")
    store.fail(job, "legacy_failure", 401, "legacy-native")
    with sqlite3.connect(store.path) as db:
        db.execute("ALTER TABLE quests DROP COLUMN session_call_count")
    restarted = TimelineSummaryStore(store.path)
    retry = restarted.claim(701, "zh")
    assert retry["native_session_ref"] == "legacy-native"
    prepared = restarted.prepare_invocation(retry)
    assert prepared["job_ref"] == retry["job_ref"]
    assert prepared["nodes"] == retry["nodes"]
    assert prepared["native_session_ref"] is None
