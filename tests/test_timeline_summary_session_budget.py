from meta_research.timeline_summaries import TimelineSummaryStore


def test_recorder_keeps_long_lived_native_root_with_short_changes(tmp_path):
    store = TimelineSummaryStore(tmp_path / "summary.sqlite3")
    for i in range(20):
        store.sync("quest-1", [{"node_key": "stage:c1:bundle", "kind": "stage",
            "source_hash": str(i), "sources": [{"ref": "source-1"}],
            "reader": {"source_ref": "source-1"}}], i * 400)
        job = store.claim(i * 400, "zh")
        assert job["native_session_ref"] == (None if i == 0 else "native-recorder")
        assert "content" not in job["nodes"][0]
        store.publish(job, [{"node_key": "stage:c1:bundle", "source_hash": str(i),
            "summary": "分析持续推进，仍需外部验证。", "source_refs": ["source-1"]}],
            "native-recorder", i * 400 + 1)
    assert store.query("quest-1")["nodes"][0]["status"] == "ready"


def test_failed_job_retry_retains_observation_across_restart(tmp_path):
    path = tmp_path / "summary.sqlite3"
    store = TimelineSummaryStore(path)
    store.sync("quest-1", [{"node_key": "stage:c1:bundle", "kind": "stage",
        "source_hash": "original", "sources": [{"ref": "source-1"}]}], 100)
    job = store.claim(100, "zh")
    store.fail(job, "timeline_summary_source_unavailable", 101, "native-recorder")
    assert TimelineSummaryStore(path).claim(400, "zh") is None
    retried = TimelineSummaryStore(path).claim(401, "en")
    assert retried["job_ref"] == job["job_ref"]
    assert retried["nodes"] == job["nodes"]
    assert retried["observed_at"] == job["observed_at"]
    assert retried["output_language"] == "zh"
    assert retried["native_session_ref"] == "native-recorder"
