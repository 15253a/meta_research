"""Exercise the installed deterministic ledger CLI with actual fulltext files."""
import json
import subprocess
import sys
from pathlib import Path

import meta_research


SCRIPT = Path(meta_research.__file__).parent / "skills/deepfetch_v4/scripts/papers.py"


def run(out, command, *args, ok=True):
    result = subprocess.run([sys.executable, str(SCRIPT), command, "--out-dir", str(out), *args],
        text=True, capture_output=True)
    assert (result.returncode == 0) == ok, result.stdout + result.stderr
    return json.loads(result.stdout) if ok else result.stderr


def test_reader_retry_retains_slot_and_exact_task_file_contract(tmp_path):
    out = tmp_path / "output"
    run(out, "init", "--topic", "Transfer under fixed low-temperature protocol")
    intake = tmp_path / "intake.json"
    intake.write_text(json.dumps([{"title": f"Paper {i}", "doi": f"10.1000/test{i}"}
        for i in range(11)]))
    ids = run(out, "upsert", "--input", str(intake))["upserted"]
    fulltext = tmp_path / "article.html"
    fulltext.write_text("<!doctype html><html><body><article>Actual article evidence.</article></body></html>")
    for paper_id in ids[:10]:
        run(out, "register-fulltext", "--paper-id", paper_id, "--file", str(fulltext))
    task = "Compare transfer; preserve the fixed low-temperature protocol and cohort."
    run(out, "prepare-readers", "--task", task)
    paths = sorted((out / ".deepfetch/reader-jobs").glob("*.json"))
    jobs = [json.loads(path.read_text()) for path in paths]
    assert len(jobs) == 10
    for job in jobs:
        assert job["task"] == task
        assert Path(job["fulltext"]["path"]).read_bytes() == fulltext.read_bytes()
        assert Path(job["reading_contract_path"]).is_file()
        assert job["paper"]["identity"]["paper_id"] == job["paper_id"]
    run(out, "prepare-readers", "--task", task)
    assert sorted((out / ".deepfetch/reader-jobs").glob("*.json")) == paths
    failed = jobs[0]
    patch = failed["patch_template"] | {"status": "failure", "reading": None,
        "error": {"type": "file_invalid", "detail": "The provided article is incomplete."}}
    patch_path = tmp_path / "failed.json"
    patch_path.write_text(json.dumps(patch))
    run(out, "apply-reader", "--result", str(patch_path))
    # A failed Reader consumes its original paper's slot; an eleventh is rejected.
    error = run(out, "register-fulltext", "--paper-id", ids[10], "--file", str(fulltext), ok=False)
    assert "10 distinct papers" in error
    run(out, "register-fulltext", "--paper-id", failed["paper_id"], "--file", str(fulltext))
    run(out, "prepare-readers", "--task", task)
    retry_jobs = [json.loads(path.read_text()) for path in (out / ".deepfetch/reader-jobs").glob("*.json")]
    assert len({job["paper_id"] for job in retry_jobs}) == 10
    assert len([job for job in retry_jobs if job["paper_id"] == failed["paper_id"]]) == 2


def test_discovery_cutoff_drains_frozen_inflight_targets_without_promoting_backups(tmp_path):
    out = tmp_path / "output"
    run(out, "init", "--topic", "Bounded comparison", "--intensity", "low")
    intake = tmp_path / "intake.json"
    intake.write_text(json.dumps([{"title": f"Paper {i}", "doi": f"10.1000/cutoff{i}"} for i in range(4)]))
    ids = run(out, "upsert", "--input", str(intake))["upserted"]
    run(out, "record-failure", "--paper-id", ids[0], "--detail", "No accessible fulltext before cutoff")
    fulltext = tmp_path / "article.html"
    fulltext.write_text("<!doctype html><html><body><article>Fulltext for selected work.</article></body></html>")
    run(out, "register-fulltext", "--paper-id", ids[1], "--file", str(fulltext))
    run(out, "update-run", "--elapsed", "480", "--stopping-reason", "Active budget exhausted",
        "--freeze-paper-id", ids[2])
    # Existing obtained work and explicitly in-flight retrieval can finish after cutoff.
    run(out, "register-fulltext", "--paper-id", ids[2], "--file", str(fulltext))
    run(out, "prepare-readers", "--task", "Read only frozen targets")
    jobs = [json.loads(path.read_text()) for path in (out / ".deepfetch/reader-jobs").glob("*.json")]
    assert {job["paper_id"] for job in jobs} == {ids[1], ids[2]}
    assert "only frozen targets" in run(out, "register-fulltext", "--paper-id", ids[3], "--file", str(fulltext), ok=False)
    assert "cannot expand" in run(out, "update-run", "--stopping-reason", "Extend drain",
        "--freeze-paper-id", ids[3], ok=False)
    intake.write_text(json.dumps({"title": "Late candidate", "doi": "10.1000/late"}))
    assert "cannot add new candidates" in run(out, "upsert", "--input", str(intake), ok=False)


def test_search_budget_overrun_is_recorded_without_freezing_discovery(tmp_path):
    out = tmp_path / "output"
    run(out, "init", "--topic", "Continue valuable discovery", "--intensity", "low")
    for elapsed in (480, 600):
        observed = run(out, "update-run", "--elapsed", str(elapsed))
        assert observed["active_search_elapsed_seconds"] == elapsed
        intake = tmp_path / f"intake-{elapsed}.json"
        intake.write_text(json.dumps({"title": f"New evidence {elapsed}", "doi": f"10.1000/value{elapsed}"}))
        assert len(run(out, "upsert", "--input", str(intake))["upserted"]) == 1
    assert run(out, "validate")["valid"] is True
    assert "cannot decrease" in run(out, "update-run", "--elapsed", "599", ok=False)
