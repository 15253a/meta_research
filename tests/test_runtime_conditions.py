"""Read actual SQLite source columns; only conditions JSON may be changed."""

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
import threading

import pytest

from meta_research.owners.common import OwnerConflict, canonical_hash
from meta_research.runtime_conditions import (
    compose_runtime_prompt, read_runtime_conditions, render_runtime_conditions,
    save_runtime_conditions, split_runtime_prompt,
)


@pytest.fixture
def root(tmp_path):
    (tmp_path / "data-root.json").write_text("{}")
    with sqlite3.connect(tmp_path / "meta-research.sqlite3") as db:
        db.executescript("""
            CREATE TABLE rg_quests(quest_ref TEXT PRIMARY KEY, initialization_id TEXT UNIQUE,
                goal_json TEXT, draft_hash TEXT);
            CREATE TABLE hc_quest_initializations(initialization_id TEXT PRIMARY KEY,
                draft_json TEXT, draft_hash TEXT);
            CREATE TABLE hc_resource_envelopes(envelope_ref TEXT PRIMARY KEY,
                initialization_id TEXT, envelope_json TEXT, envelope_hash TEXT);
            CREATE TABLE ar_run_controls(run_ref TEXT PRIMARY KEY, quest_ref TEXT);
            CREATE TABLE ar_harness_runs(run_ref TEXT PRIMARY KEY, request_json TEXT, request_hash TEXT);
            CREATE TABLE rg_targets(target_ref TEXT PRIMARY KEY, graph_ref TEXT);
            CREATE TABLE rg_target_graphs(graph_ref TEXT PRIMARY KEY, quest_ref TEXT);
            CREATE TABLE ar_deepfetch_runs(run_ref TEXT PRIMARY KEY, request_ref TEXT);
            CREATE TABLE hc_deepfetch_requests(request_ref TEXT PRIMARY KEY, initialization_id TEXT);
            CREATE TABLE hc_manual_deepfetch_requests(request_ref TEXT PRIMARY KEY,
                initialization_id TEXT, quest_ref TEXT);
        """)
        for name, count, accepted in (("one", 4, True), ("two", 1, True), ("draft", 0, False)):
            devices = [{"uuid": f"GPU-{name}-{i}", "name": "NVIDIA A100-SXM4-80GB",
                        "memory_total_mib": 81920} for i in range(count)]
            envelope = {"time_budget": "30d", "selected_devices": devices,
                        "selected_device_uuids": [d["uuid"] for d in devices]}
            draft = {"goal": "Research", "time_budget": "30d",
                     "resource_envelope_ref": f"envelope-{name}" if count else None,
                     "resource_envelope_hash": canonical_hash(envelope) if count else None,
                     "literature": {"mode": "oa_only", "scope_exclusions": "排除非同行评审来源",
                                    "library_entry_url": "secret-url", "auth_path": "secret-auth"}}
            db.execute("INSERT INTO hc_quest_initializations VALUES (?,?,?)",
                       (f"init-{name}", json.dumps(draft), canonical_hash(draft)))
            if count:
                db.execute("INSERT INTO hc_resource_envelopes VALUES (?,?,?,?)",
                           (f"envelope-{name}", f"init-{name}", json.dumps(envelope), canonical_hash(envelope)))
            if accepted:
                db.execute("INSERT INTO rg_quests VALUES (?,?,?,?)",
                           (f"quest-{name}", f"init-{name}", json.dumps(draft), canonical_hash(draft)))
                db.execute("INSERT INTO ar_run_controls VALUES (?,?)", (f"run-{name}", f"quest-{name}"))
                db.execute("INSERT INTO rg_target_graphs VALUES (?,?)", (f"graph-{name}", f"quest-{name}"))
                db.execute("INSERT INTO rg_targets VALUES (?,?)", (f"target-{name}", f"graph-{name}"))
        db.execute("INSERT INTO ar_run_controls VALUES ('deepfetch-draft',NULL)")
        db.execute("INSERT INTO ar_deepfetch_runs VALUES ('deepfetch-draft','request-draft')")
        db.execute("INSERT INTO hc_deepfetch_requests VALUES ('request-draft','init-draft')")
        db.execute("INSERT INTO ar_deepfetch_runs VALUES ('deepfetch-manual','request-manual')")
        db.execute("INSERT INTO hc_manual_deepfetch_requests VALUES ('request-manual','init-one','quest-one')")
        request = {"target_ref": "target-one", "target_run_ref": "harness-one"}
        db.execute("INSERT INTO ar_harness_runs VALUES (?,?,?)",
                   ("harness-one", json.dumps(request), canonical_hash(request)))
    return tmp_path


def test_selected_four_gpus_budget_literature_and_target_directory(root):
    workspace = root / "run" / "target-workspaces" / "target-one"
    workspace.mkdir(parents=True)
    value = read_runtime_conditions(workspace, "quest-one")
    fields = json.loads(value["text"].split("\n", 1)[1])
    assert len(fields["selected_devices"]) == 4
    assert fields["selected_device_uuids"] == [f"GPU-one-{i}" for i in range(4)]
    assert all(d["memory_total_mib"] == 81920 for d in fields["selected_devices"])
    assert fields["time_budget"] == "30d"
    assert fields["literature"] == {"mode": "oa_only", "scope_exclusions": "排除非同行评审来源"}
    assert "secret-" not in value["text"]
    rendered = render_runtime_conditions(workspace, quest_ref="quest-one", run_ref="run-one",
                                         target_ref="target-one", initialization_id="init-one")
    assert "GPU-one-3" in rendered


def test_edits_persist_refresh_and_leave_other_quest_and_database_unchanged(root):
    database = root / "meta-research.sqlite3"
    before = database.read_bytes()
    original = read_runtime_conditions(root, "quest-one")
    other = read_runtime_conditions(root, "quest-two")
    saved = save_runtime_conditions(root, "quest-one", text="预算七天，只使用 GPU-one-0。",
                                    expected_revision=original["revision"])
    assert read_runtime_conditions(Path(str(root)), "quest-one") == saved
    assert "预算七天" in render_runtime_conditions(root, run_ref="run-one")
    assert read_runtime_conditions(root, "quest-two") == other
    assert "预算七天" not in render_runtime_conditions(root, run_ref="run-two")
    assert database.read_bytes() == before


@pytest.mark.parametrize("scope", [
    {"quest_ref": "quest-one", "run_ref": "run-two"},
    {"quest_ref": "quest-one", "target_ref": "target-two"},
    {"quest_ref": "quest-one", "initialization_id": "init-two"},
    {"run_ref": "run-one", "target_ref": "target-two"},
    {"quest_ref": "quest-one", "run_ref": "deepfetch-draft"},
    {"run_ref": "deepfetch-draft", "initialization_id": "init-two"},
])
def test_conflicting_scopes_are_rejected(root, scope):
    with pytest.raises(OwnerConflict, match="runtime_conditions_scope_conflict"):
        render_runtime_conditions(root, **scope)


@pytest.mark.parametrize("scope,reason", [
    ({"quest_ref": "quest-missing"}, "quest_not_found"),
    ({"run_ref": "run-missing", "quest_ref": "quest-one"}, "run_not_found"),
    ({"target_ref": "target-missing", "quest_ref": "quest-one"}, "target_not_found"),
    ({"initialization_id": "init-missing"}, "initialization_not_found"),
])
def test_unknown_scopes_never_fall_back_to_current_quest(root, scope, reason):
    with pytest.raises(OwnerConflict, match=reason):
        render_runtime_conditions(root, **scope)


@pytest.mark.parametrize("table,column,where", [
    ("rg_quests", "goal_json", "quest_ref='quest-one'"),
    ("hc_quest_initializations", "draft_json", "initialization_id='init-draft'"),
    ("hc_resource_envelopes", "envelope_json", "initialization_id='init-one'"),
])
def test_corrupt_source_content_is_rejected(root, table, column, where):
    with sqlite3.connect(root / "meta-research.sqlite3") as db:
        db.execute(f"UPDATE {table} SET {column}='{{}}' WHERE {where}")
    scope = {"initialization_id": "init-draft"} if table == "hc_quest_initializations" else {"quest_ref": "quest-one"}
    with pytest.raises(OwnerConflict, match="runtime_conditions_(draft|resource)_invalid"):
        render_runtime_conditions(root, **scope)


def test_deepfetch_initialization_and_manual_quest_are_resolved(root):
    assert "未配置已选 GPU" in render_runtime_conditions(root, run_ref="deepfetch-draft")
    assert "GPU-one-3" in render_runtime_conditions(root, run_ref="deepfetch-manual")


def test_real_target_harness_without_run_control_resolves_exact_target(root):
    rendered = render_runtime_conditions(root, run_ref="harness-one", target_ref="target-one")
    assert "GPU-one-3" in rendered
    assert render_runtime_conditions(root, run_ref="harness-one") == rendered
    with pytest.raises(OwnerConflict, match="runtime_conditions_scope_conflict"):
        render_runtime_conditions(root, run_ref="harness-one", target_ref="target-two")
    with sqlite3.connect(root / "meta-research.sqlite3") as db:
        db.execute("UPDATE ar_harness_runs SET request_json='{}' WHERE run_ref='harness-one'")
    with pytest.raises(OwnerConflict, match="runtime_conditions_run_invalid"):
        render_runtime_conditions(root, run_ref="harness-one", target_ref="target-one")


def test_initialization_edits_survive_acceptance_without_changing_other_scope(root):
    scope = "initialization:init-draft"
    original = read_runtime_conditions(root, scope)
    save_runtime_conditions(root, scope, text="仅使用 CPU，预算三天。", expected_revision=original["revision"])
    assert "仅使用 CPU" in render_runtime_conditions(root, run_ref="deepfetch-draft")
    with sqlite3.connect(root / "meta-research.sqlite3") as db:
        db.execute("INSERT INTO rg_quests SELECT 'quest-new',initialization_id,draft_json,draft_hash "
                   "FROM hc_quest_initializations WHERE initialization_id='init-draft'")
    inherited = read_runtime_conditions(root, "quest-new")
    assert inherited["text"] == "仅使用 CPU，预算三天。"
    assert read_runtime_conditions(root, scope) == inherited
    save_runtime_conditions(root, scope, text="继续 CPU，预算四天。", expected_revision=inherited["revision"])
    assert read_runtime_conditions(root, "quest-new")["text"] == "继续 CPU，预算四天。"
    assert "GPU-one-3" in read_runtime_conditions(root, "quest-one")["text"]


def test_concurrent_updates_have_one_winner_and_one_stale_revision(root):
    original = read_runtime_conditions(root, "quest-one")
    barrier = threading.Barrier(2)
    def update(text):
        barrier.wait(timeout=10)
        try:
            return save_runtime_conditions(root, "quest-one", text=text,
                                           expected_revision=original["revision"])
        except OwnerConflict as error:
            return str(error)
    with ThreadPoolExecutor(max_workers=2) as pool:
        values = list(pool.map(update, ("只用第一张卡", "只用第二张卡")))
    assert sum(isinstance(value, dict) for value in values) == 1
    assert "runtime_conditions_stale" in values
    assert read_runtime_conditions(root, "quest-one") in values


def test_empty_or_oversized_edit_is_rejected_without_new_file(root):
    original = read_runtime_conditions(root, "quest-one")
    for value in (" ", "a" * 24001):
        with pytest.raises(OwnerConflict, match="runtime_conditions_text_invalid"):
            save_runtime_conditions(root, "quest-one", text=value, expected_revision=original["revision"])
    assert not (root / "runtime-conditions").exists()


def test_prompt_framing_round_trip_and_invalid_length():
    conditions = "条件：四张显卡\n\n以及预算。"
    prompt = "实际任务\n[meta-research runtime conditions v1; characters=2]"
    assert split_runtime_prompt(compose_runtime_prompt(prompt, conditions)) == (conditions, prompt)
    assert split_runtime_prompt(prompt) == ("", prompt)
    with pytest.raises(ValueError, match="runtime_conditions_prompt_invalid"):
        split_runtime_prompt("[meta-research runtime conditions v1; characters=900]\n短文本")
