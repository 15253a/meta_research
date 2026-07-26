"""File-side replay closure: ContextPack -> Artifact/handoff -> cycle_report."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from orchestrator import database as db
from orchestrator.cycle_replay import CycleReplayArchive, CycleReplayError
from orchestrator.interfaces import Artifact, CallUsage, ContextPack, ManagedArtifactRef
from orchestrator.run import build_system


def _pack(cycle_id="c1", stage="reasoning", *, anchor="固定锚"):
    pack = ContextPack(
        cycle_id=cycle_id, stage=stage, target_id=None,
        anchor_md=anchor, neighborhood_md="祖先", retrieval_md="召回",
        refs=["opaque:r1"], sources=["db:question:1", "policy:budget"])
    pack.pack_hash = hashlib.sha256(("\x00".join((
        pack.anchor_md, pack.neighborhood_md, pack.retrieval_md,
        json.dumps(pack.refs, ensure_ascii=False),
    ))).encode("utf-8")).hexdigest()
    return pack


def _reasoning(md="本轮以有效证据回答了问题，并选择下一题。"):
    return Artifact(
        stage="reasoning",
        files={
            "selection.json": {
                "next_question_id": None, "next_intent": "terminate",
                "terminate_reason_md": "目标满足",
            },
            "tree_ops.json": {"ops": []},
        },
        md=md,
        prompt_sha256="sha256:" + "1" * 64,
        transcript_ref="cycles/c1/transcripts/reasoning.events.jsonl",
    )


def test_exact_reasoning_md_is_promoted_and_whole_cycle_is_hashed(tmp_path):
    archive = CycleReplayArchive(tmp_path)
    pack = _pack()
    archive.persist_context_pack(pack)
    result = archive.persist_stage_artifact(
        cycle_id="c1", stage="reasoning", artifact=_reasoning(),
        pack_hash=pack.pack_hash, runner_call_id=7)
    assert result["handoff_no"] == 1

    closure = archive.finalize_cycle(
        cycle_id="c1", status="done", route="bootstrap",
        next_intent="terminate")
    cycle = tmp_path / "cycles" / "c1"
    expected = _reasoning().md.encode("utf-8")
    assert (cycle / "cycle_report.md").read_bytes() == expected
    assert (cycle / "artifacts" / "reasoning.md").read_bytes() == expected
    assert (cycle / "handoff-1.md").is_file()
    assert closure["coverage"] == {
        "context_pack": True, "stage_artifacts": True, "handoff": True,
        "cycle_report": True, "cycle_state": False,
        "legacy_incomplete": True,
    }
    assert archive.verify_cycle("c1")["report_kind"] == "reasoning_md_promoted"

    # Same calls are byte-idempotent and do not allocate another handoff.
    archive.persist_context_pack(pack)
    again = archive.persist_stage_artifact(
        cycle_id="c1", stage="reasoning", artifact=_reasoning(),
        pack_hash=pack.pack_hash, runner_call_id=7)
    assert again["handoff_no"] == 1
    assert list(cycle.glob("handoff-*.md")) == [cycle / "handoff-1.md"]
    assert archive.finalize_cycle(
        cycle_id="c1", status="done", route="bootstrap",
        next_intent="terminate") == closure


def test_reasoning_main_purpose_is_promoted(tmp_path):
    archive = CycleReplayArchive(tmp_path)
    archive.persist_stage_artifact(
        cycle_id="c1", stage="reasoning",
        artifact=_reasoning(md="ACTUAL REASONING"),
        purpose="reasoning-main-c1", pack_hash="a" * 64)

    closure = archive.finalize_cycle(
        cycle_id="c1", status="done", route="attack")

    assert closure["report_kind"] == "reasoning_md_promoted"
    assert closure["source_event_id"]
    assert (tmp_path / "cycles" / "c1" / "cycle_report.md").read_text(
        encoding="utf-8") == "ACTUAL REASONING"


def test_done_cycle_without_reasoning_event_cannot_be_sealed(tmp_path):
    archive = CycleReplayArchive(tmp_path)

    with pytest.raises(CycleReplayError, match="缺 reasoning"):
        archive.finalize_cycle(
            cycle_id="c1", status="done", route="attack")


def test_context_alias_and_history_preserve_exact_four_regions(tmp_path):
    archive = CycleReplayArchive(tmp_path)
    pack = _pack(stage="plan")
    result = archive.persist_context_pack(pack, label="plan-review-1")
    root = tmp_path / "cycles" / "c1" / "context_pack"
    exact = json.loads((root / "plan.plan-review-1.pack.json").read_text(encoding="utf-8"))
    manifest = json.loads(
        (root / "plan.plan-review-1.manifest.json").read_text(encoding="utf-8"))
    assert exact["anchor_md"] == "固定锚"
    assert exact["refs"] == ["opaque:r1"]
    assert manifest["pack_hash"] == result["pack_hash"]
    assert (root / manifest["history_ref"]).is_file()


def test_bundle_replay_indexes_managed_file_without_copying_body(tmp_path):
    archive = CycleReplayArchive(tmp_path)
    pack = _pack(stage="bundle")
    pack.target_id = "7"
    managed = (tmp_path / "cycles" / "c1" / "artifacts" / "managed-files" /
               "bundle-7" / "train.py")
    managed.parent.mkdir(parents=True)
    raw = b"print('large managed source')\n" * 20_000
    managed.write_bytes(raw)
    ref = ManagedArtifactRef(
        path=str(managed), size_bytes=len(raw),
        sha256="sha256:" + hashlib.sha256(raw).hexdigest())
    artifact = Artifact(
        stage="bundle",
        files={"execution_manifest.json": {"manifest_version": 1},
               "identity.md": "# identity", "train.py": ref})

    result = archive.persist_stage_artifact(
        cycle_id="c1", stage="bundle", artifact=artifact,
        target_id="7", purpose="bundle", pack_hash=pack.pack_hash,
        runner_call_id=9)

    cycle = tmp_path / "cycles" / "c1"
    event = cycle / "artifacts" / "history" / result["event_id"]
    pointer = json.loads((event / "managed-files.json").read_text(encoding="utf-8"))
    assert pointer["files"][0]["managed_ref"].endswith(
        "artifacts/managed-files/bundle-7/train.py")
    assert not (event / "files" / "train.py").exists()
    assert not (cycle / "artifacts" / "by-stage" / "bundle.7" / "train.py").exists()
    assert list(cycle.rglob("train.py")) == [managed]


def _runtime_submission_fixture(tmp_path, *, registered=True, outside=False):
    conn = db.connect(str(tmp_path / "research.sqlite"))
    conn.execute(
        "INSERT INTO goal(id,version,text,predicate_json) VALUES (1,1,'目标','{}')")
    conn.execute(
        "INSERT INTO cycle(id,goal_id,goal_ver,status,policy_version) "
        "VALUES (1,1,1,'plan','p1')")
    purpose = "bundle-main-c1-n1-a1"
    conn.execute(
        "INSERT INTO runner_call(id,cycle_id,phase,purpose,status) "
        "VALUES (9,1,'bundle',?,'running')", (purpose,))
    pack = _pack(stage="bundle")
    pack.target_id = "7"
    submission = (tmp_path / "runtime" / "stage-submissions" / "c1" /
                  "bundle" / "t7" / "s123")
    managed = ((tmp_path / "outside" / "train.py") if outside else
               (submission / "bundle" / "train.py"))
    managed.parent.mkdir(parents=True)
    raw = b"print('runtime MCP managed source')\n"
    managed.write_bytes(raw)
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    entry = {
        "name": "train.py", "kind": "managed", "path": str(managed),
        "size_bytes": len(raw), "sha256": digest,
    }
    descriptor = {
        "files": [{key: entry[key] for key in (
            "name", "kind", "size_bytes", "sha256")}],
        "md": None,
    }
    artifact_hash = "sha256:" + hashlib.sha256(
        (json.dumps(descriptor, ensure_ascii=False, sort_keys=True,
                    separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
    ).hexdigest()
    receipt = {
        "protocol": "runtime-stage-submission-v1",
        "cycle_id": "c1", "stage": "bundle", "target_id": "7",
        "purpose": purpose, "pack_hash": pack.pack_hash,
        "submission_kind": "bundle", "review_decision_id": None,
        "revision": 1, "artifact_hash": artifact_hash,
        "files": [entry], "md": None,
    }
    receipt_path = submission / "submission.json"
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    receipt_path.write_text(
        json.dumps(receipt, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8")
    receipt_hash = "sha256:" + hashlib.sha256(receipt_path.read_bytes()).hexdigest()
    if registered:
        conn.execute(
            "INSERT INTO decision(cycle_id,actor,type,payload_json) "
            "VALUES (1,'agent','runtime_stage_submission',?)",
            (json.dumps({
                "protocol": "runtime-stage-submission-index-v1",
                "stage": "bundle", "target_id": "7", "purpose": purpose,
                "revision": 1, "artifact_hash": artifact_hash,
                "submission_ref": str(receipt_path),
                "submission_hash": receipt_hash,
                "file_names": ["train.py"],
            }, ensure_ascii=False, sort_keys=True),))
    conn.commit()
    artifact = Artifact(
        stage="bundle", files={"train.py": ManagedArtifactRef(
            path=str(managed), size_bytes=len(raw), sha256=digest)},
        stage_submission_ref=str(receipt_path),
        stage_submission_hash=artifact_hash)
    return conn, pack, artifact


def test_bundle_replay_accepts_registered_runtime_mcp_managed_file(tmp_path):
    conn, pack, artifact = _runtime_submission_fixture(tmp_path)
    archive = CycleReplayArchive(tmp_path, submission_registry=conn)
    result = archive.persist_stage_artifact(
        cycle_id="c1", stage="bundle", artifact=artifact,
        target_id="7", purpose="bundle-main-c1",
        pack_hash=pack.pack_hash, runner_call_id=9)

    pointer = json.loads((
        tmp_path / "cycles" / "c1" / "artifacts" / "history" /
        result["event_id"] / "managed-files.json").read_text(encoding="utf-8"))
    assert pointer["files"][0]["managed_ref"].startswith(
        "runtime/stage-submissions/c1/bundle/t7/")
    assert list((tmp_path / "cycles" / "c1").rglob("train.py")) == []
    conn.close()


def test_bundle_replay_rejects_unregistered_runtime_mcp_managed_file(tmp_path):
    conn, pack, artifact = _runtime_submission_fixture(tmp_path, registered=False)
    archive = CycleReplayArchive(tmp_path, submission_registry=conn)
    with pytest.raises(CycleReplayError, match="未由当前 quest MCP 唯一登记"):
        archive.persist_stage_artifact(
            cycle_id="c1", stage="bundle", artifact=artifact,
            target_id="7", purpose="bundle-main-c1",
            pack_hash=pack.pack_hash, runner_call_id=9)
    conn.close()


def test_bundle_replay_rejects_registered_receipt_pointing_outside_quest_manager(
        tmp_path):
    conn, pack, artifact = _runtime_submission_fixture(tmp_path, outside=True)
    archive = CycleReplayArchive(tmp_path, submission_registry=conn)
    with pytest.raises(CycleReplayError, match="不在受信 cycle/MCP 文件管理区"):
        archive.persist_stage_artifact(
            cycle_id="c1", stage="bundle", artifact=artifact,
            target_id="7", purpose="bundle-main-c1",
            pack_hash=pack.pack_hash, runner_call_id=9)
    conn.close()


def test_terminal_closure_rejects_new_turn_and_detects_extra_file(tmp_path):
    archive = CycleReplayArchive(tmp_path)
    pack = _pack()
    archive.persist_context_pack(pack)
    archive.persist_stage_artifact(
        cycle_id="c1", stage="reasoning", artifact=_reasoning(),
        pack_hash=pack.pack_hash)
    archive.finalize_cycle(cycle_id="c1", status="done", route="bootstrap")

    with pytest.raises(CycleReplayError, match="拒绝追加新 turn"):
        archive.persist_stage_output(
            cycle_id="c1", stage="reasoning",
            files={"selection.json": {"next_intent": "attack"}}, md="另一份正文")

    (tmp_path / "cycles" / "c1" / "untracked.txt").write_text("漂移", encoding="utf-8")
    with pytest.raises(CycleReplayError, match="inventory 漂移"):
        archive.verify_cycle("c1")


def test_sqlite_reconcile_synthesizes_legacy_report_and_skips_worker_cycle(tmp_path):
    conn = db.connect(str(tmp_path / "research.sqlite"))
    conn.execute(
        "INSERT INTO goal(id,version,text,predicate_json) VALUES (1,1,'目标','{}')")
    conn.execute(
        "INSERT INTO cycle(id,goal_id,goal_ver,status,route,policy_version,next_intent) "
        "VALUES (1,1,1,'failed','dependency_wait','p1','attack')")
    conn.execute(
        "INSERT INTO cycle(id,goal_id,goal_ver,status,policy_version) "
        "VALUES (2,1,1,'done','p1')")
    conn.execute(
        "INSERT INTO decision(cycle_id,actor,type,payload_json) "
        "VALUES (2,'orchestrator','import_worker_cycle','{}')")
    conn.commit()

    archive = CycleReplayArchive(tmp_path)
    assert archive.reconcile_sqlite(conn) == ["c1"]
    report = (tmp_path / "cycles" / "c1" / "cycle_report.md").read_text(encoding="utf-8")
    assert "编排器根据终态元数据机械生成" in report
    assert "dependency_wait" in report
    assert not (tmp_path / "cycles" / "c2" / "cycle_report.md").exists()
    assert archive.reconcile_sqlite(conn) == []


def _seed_terminal_cycle_state(
        conn, *, omit_bundle_commit=None, omit_scientific=None,
        target_status_overrides=None, scientific_overrides=None):
    conn.execute(
        "INSERT INTO goal(id,version,text,predicate_json) VALUES (1,1,'目标','{}')")
    conn.execute(
        "INSERT INTO cycle(id,goal_id,goal_ver,status,route,policy_version,next_intent) "
        "VALUES (1,1,1,'done','attack','p1','terminate')")
    conn.execute(
        "INSERT INTO question(id,goal_id,goal_ver,born_goal_ver,text,status,source,born_cycle) "
        "VALUES (1,1,1,1,'问题','active','agent',1)")
    conn.execute("UPDATE cycle SET active_question_id=1 WHERE id=1")
    conn.execute(
        "INSERT INTO baseline(id,slug,canonical_key,born_cycle,status) "
        "VALUES (20,'replay-b','replay-b',1,'legal')")
    conn.execute(
        "INSERT INTO variant(id,baseline_id,variant_key,config_json,born_question,status) "
        "VALUES (21,20,'v1','{}',1,'legal')")
    targets = [
        (11, "complete", None, 20, 21),
        (12, "failed", "runtime", None, None),
        (13, "skipped", None, None, None),
        (14, "engineering_blocked", "smoke", None, None),
    ]
    target_status_overrides = target_status_overrides or {}
    scientific_overrides = scientific_overrides or {}
    for seq, (target_id, status, failure_kind, baseline_id, variant_id) in enumerate(
            targets, 1):
        status = target_status_overrides.get(target_id, status)
        conn.execute(
            "INSERT INTO build_target("
            "id,cycle_id,question_id,target_kind,seq,status,failure_kind,"
            "baseline_id,variant_id) VALUES (?,1,1,'exec',?,?,?,?,?)",
            (target_id, seq, status, failure_kind, baseline_id, variant_id))
        if target_id != omit_bundle_commit:
            conn.execute(
                "INSERT INTO phase_commit(cycle_id,stage,target_id,artifact_hash) "
                "VALUES (1,'bundle',?,?)", (target_id, f"bundle-{target_id}"))
        if target_id == omit_scientific:
            continue
        scientific = {
            "protocol": (
                "bundle-scientific-contract-v1" if target_id == 11
                else "bundle-scientific-terminal-v1"),
            "build_target_id": target_id,
            "execution_status": {
                11: "succeeded", 12: "failed", 13: "skipped",
                14: "engineering_blocked",
            }[target_id],
            "validity_status": "valid" if target_id == 11 else "not_assessed",
            "scientific_outcome": "refuted" if target_id == 11 else "unavailable",
            "pool_eligibility": "eligible" if target_id == 11 else "ineligible",
        }
        if target_id != 11:
            scientific.update({
                "target_status": status,
                "failure_kind": failure_kind,
                "contract_hash": "0" * 64,
            })
        scientific.update(scientific_overrides.get(target_id, {}))
        conn.execute(
            "INSERT INTO decision(cycle_id,question_id,actor,type,payload_json) "
            "VALUES (1,1,'orchestrator',?,?)",
            ("bundle_scientific_contract" if target_id == 11
             else "bundle_scientific_terminal",
             json.dumps(scientific, ensure_ascii=False, sort_keys=True)))
    review = {
        "protocol": "native-review-receipt-v1",
        "cycle_id": "c1", "stage": "bundle", "target_id": "11",
        "review_kind": "bundle_code", "round_no": 1, "configured_rounds": 1,
        "verdict": "pass", "child_thread_id": "child-review-11",
        "receipt_hash": "sha256:" + "a" * 64,
    }
    review_id = conn.execute(
        "INSERT INTO decision(cycle_id,question_id,actor,type,payload_json) "
        "VALUES (1,1,'agent','runtime_review',?)",
        (json.dumps(review, ensure_ascii=False, sort_keys=True),)).lastrowid
    conn.execute(
        "INSERT INTO decision(cycle_id,question_id,actor,type,payload_json) "
        "VALUES (1,1,'orchestrator','runtime_bundle_result_review_ack',?)",
        (json.dumps({
            "protocol": "native-bundle-result-review-ack-v1",
            "build_target_id": 11, "review_decision_id": review_id,
            "review_receipt_hash": review["receipt_hash"],
        }, ensure_ascii=False, sort_keys=True),))
    conn.execute(
        "INSERT INTO decision(cycle_id,question_id,actor,type,payload_json) "
        "VALUES (1,1,'gate','pool_publication',?)",
        (json.dumps({
            "schema": "pool-db-binding-v1", "baseline_id": 20, "variant_id": 21,
            "manifest_ref": "formal/pool-11.json",
            "manifest_hash": "sha256:" + "b" * 64,
        }, ensure_ascii=False, sort_keys=True),))
    conn.execute(
        "INSERT INTO decision(cycle_id,question_id,actor,type,payload_json) "
        "VALUES (1,1,'agent','runtime_cycle_summary',?)",
        (json.dumps({
            "protocol": "runtime-cycle-summary-v1", "question_id": 1,
            "decision": "inconclusive", "conclusion_md": "耐久 Reasoning 原文",
            "next_step_md": "进入下一轮", "evidence_refs": [],
        }, ensure_ascii=False, sort_keys=True),))
    conn.execute(
        "INSERT INTO phase_commit(cycle_id,stage,target_id,artifact_hash) "
        "VALUES (1,'reasoning',NULL,'reasoning-1')")
    conn.commit()


def test_reconcile_state_projection_includes_every_target_and_durable_gate_reference(
        tmp_path):
    conn = db.connect(str(tmp_path / "research.sqlite"))
    _seed_terminal_cycle_state(conn)
    archive = CycleReplayArchive(tmp_path)
    pack = _pack()
    archive.persist_stage_artifact(
        cycle_id="c1", stage="reasoning", artifact=_reasoning(),
        purpose="reasoning-main-c1", pack_hash=pack.pack_hash)

    assert archive.reconcile_sqlite(conn) == ["c1"]

    cycle = tmp_path / "cycles" / "c1"
    state = json.loads((cycle / "cycle_state.json").read_text(encoding="utf-8"))
    targets = {row["id"]: row for row in state["targets"]}
    assert {target_id: row["status"] for target_id, row in targets.items()} == {
        11: "complete", 12: "failed", 13: "skipped",
        14: "engineering_blocked",
    }
    scientific = {row["id"]: row for row in state["scientific_decisions"]}
    reviews = {row["id"]: row for row in state["review_decisions"]}
    pools = {row["id"]: row for row in state["pool_decisions"]}
    assert scientific[targets[11]["scientific_decision_ids"][0]][
        "payload"]["validity_status"] == "valid"
    assert scientific[targets[12]["scientific_decision_ids"][0]][
        "payload"]["execution_status"] == "failed"
    assert reviews[targets[11]["review_decision_ids"][0]][
        "payload"]["child_thread_id"] == "child-review-11"
    assert pools[targets[11]["pool_decision_ids"][0]][
        "payload"]["manifest_ref"] == "formal/pool-11.json"
    assert state["reasoning"]["phase_commits"][0]["artifact_hash"] == "reasoning-1"
    assert state["reasoning"]["summary_decisions"][0]["payload"]["conclusion_md"] == (
        "耐久 Reasoning 原文")
    closure = archive.verify_cycle("c1")
    assert closure["coverage"]["cycle_state"] is True
    assert "cycle_state.json" in {row["path"] for row in closure["files"]}

    # Sealed replay cannot remain "green" after a new relevant durable fact
    # makes its state projection incomplete.
    conn.execute(
        "INSERT INTO decision(cycle_id,question_id,actor,type,payload_json) "
        "VALUES (1,1,'orchestrator','bundle_scientific_terminal',?)",
        (json.dumps({
            "protocol": "bundle-scientific-terminal-v1",
            "build_target_id": 12, "execution_status": "failed",
            "validity_status": "not_assessed",
            "scientific_outcome": "unavailable",
            "pool_eligibility": "ineligible", "late_fact": True,
        }, sort_keys=True),))
    conn.commit()
    with pytest.raises(CycleReplayError, match="cycle_state.*漂移"):
        archive.reconcile_sqlite(conn)
    conn.close()


@pytest.mark.parametrize(
    ("seed_options", "message"),
    [
        (
            {"omit_bundle_commit": 12},
            "Bundle phase_commit",
        ),
        (
            {"omit_scientific": 12},
            "科学四轴",
        ),
        (
            {"target_status_overrides": {12: "running"}},
            "非终态",
        ),
        (
            {"scientific_overrides": {12: {
                "execution_status": "succeeded",
                "validity_status": "valid",
                "scientific_outcome": "supported",
                "pool_eligibility": "eligible",
            }}},
            "科学四轴.*冲突",
        ),
    ],
)
def test_done_cycle_rejects_incomplete_target_terminal_projection(
        tmp_path, seed_options, message):
    conn = db.connect(str(tmp_path / "research.sqlite"))
    _seed_terminal_cycle_state(conn, **seed_options)
    archive = CycleReplayArchive(tmp_path)
    archive.persist_stage_artifact(
        cycle_id="c1", stage="reasoning", artifact=_reasoning(),
        purpose="reasoning-main-c1", pack_hash="a" * 64)

    with pytest.raises(CycleReplayError, match=message):
        archive.reconcile_sqlite(conn)
    assert not (tmp_path / "cycles" / "c1" / "cycle_state.json").exists()
    conn.close()


def test_pool_decision_target_match_never_falls_back_after_stronger_owner_mismatch():
    targets = [
        {
            "id": 11, "baseline_id": 20, "variant_id": 21,
            "evaluation_id": 101, "evaluation_ids": [101],
            "evaluation_attempt_ids": [201], "run_ids": [301],
        },
        {
            "id": 12, "baseline_id": 20, "variant_id": 21,
            "evaluation_id": 102, "evaluation_ids": [102],
            "evaluation_attempt_ids": [202], "run_ids": [302],
        },
    ]
    evaluation_publication = {
        "payload": {
            "evaluation_id": 101, "attempt_id": 201,
            "variant_id": 21, "baseline_id": 20,
        },
    }
    training_publication = {
        "payload": {
            "run_id": 301, "variant_id": 21, "baseline_id": 20,
        },
    }

    assert CycleReplayArchive._decision_targets(
        evaluation_publication, targets, category="pool") == [11]
    assert CycleReplayArchive._decision_targets(
        training_publication, targets, category="pool") == [11]


def test_done_sqlite_cycle_requires_reasoning_phase_commit_as_well_as_stage_event(
        tmp_path):
    conn = db.connect(str(tmp_path / "research.sqlite"))
    conn.execute(
        "INSERT INTO goal(id,version,text,predicate_json) VALUES (1,1,'目标','{}')")
    conn.execute(
        "INSERT INTO cycle(id,goal_id,goal_ver,status,route,policy_version) "
        "VALUES (1,1,1,'done','bootstrap','p1')")
    conn.commit()
    archive = CycleReplayArchive(tmp_path)
    archive.persist_stage_artifact(
        cycle_id="c1", stage="reasoning", artifact=_reasoning(),
        purpose="reasoning-main-c1", pack_hash="a" * 64)

    with pytest.raises(CycleReplayError, match="Reasoning phase_commit"):
        archive.reconcile_sqlite(conn)
    conn.close()


def test_reconcile_does_not_freeze_state_before_done_reasoning_event_is_archived(
        tmp_path):
    conn = db.connect(str(tmp_path / "research.sqlite"))
    conn.execute(
        "INSERT INTO goal(id,version,text,predicate_json) VALUES (1,1,'目标','{}')")
    conn.execute(
        "INSERT INTO cycle(id,goal_id,goal_ver,status,route,policy_version) "
        "VALUES (1,1,1,'done','bootstrap','p1')")
    conn.execute(
        "INSERT INTO phase_commit(cycle_id,stage,target_id,artifact_hash) "
        "VALUES (1,'reasoning',NULL,'reasoning-1')")
    conn.commit()
    archive = CycleReplayArchive(tmp_path)

    with pytest.raises(CycleReplayError, match="reasoning stage event"):
        archive.reconcile_sqlite(conn)
    assert not (tmp_path / "cycles" / "c1" / "cycle_state.json").exists()

    archive.persist_stage_artifact(
        cycle_id="c1", stage="reasoning", artifact=_reasoning(),
        purpose="reasoning-main-c1", pack_hash="a" * 64)
    assert archive.reconcile_sqlite(conn) == ["c1"]
    conn.close()


def test_verified_sqlite_only_restore_seals_honest_missing_reasoning_projection(
        tmp_path):
    conn = db.connect(str(tmp_path / "research.sqlite"))
    conn.execute(
        "INSERT INTO goal(id,version,text,predicate_json) VALUES (1,1,'目标','{}')")
    conn.execute(
        "INSERT INTO cycle(id,goal_id,goal_ver,status,route,policy_version) "
        "VALUES (1,1,1,'done','bootstrap','p1')")
    conn.execute(
        "INSERT INTO phase_commit(cycle_id,stage,target_id,artifact_hash) "
        "VALUES (1,'reasoning',NULL,'reasoning-1')")
    conn.execute(
        "INSERT INTO decision(cycle_id,actor,type,payload_json) "
        "VALUES (1,'agent','runtime_cycle_summary',?)",
        (json.dumps({
            "protocol": "runtime-cycle-summary-v1",
            "decision": "terminate", "conclusion_md": "数据库内的耐久摘要",
            "next_step_md": "", "evidence_refs": [],
        }, ensure_ascii=False, sort_keys=True),))
    conn.commit()
    source = tmp_path / "original"
    source.mkdir()
    receipt = {
        "schema": "meta-research-storage-restore/v1",
        "scope": "sqlite_truth_only",
        "continuation_mode": "legacy_adoption_on_first_start",
        "publication_contract": "atomic_noreplace_or_lease_fenced_ready",
        "source_work_root": str(source),
        "source_cycle": "c1",
        "source_manifest_sha256": "e" * 64,
        "backup": {
            "path": "state/storage/backups/sha256/" + "f" * 64 + ".sqlite",
            "sha256": "f" * 64, "bytes": 123,
        },
    }
    restore_path = tmp_path / "restore.json"
    restore_path.write_text(
        json.dumps(
            receipt, ensure_ascii=False, sort_keys=True,
            separators=(",", ":")) + "\n",
        encoding="utf-8")
    restore_path.chmod(0o400)

    archive = CycleReplayArchive(tmp_path)
    with pytest.raises(CycleReplayError, match="恢复状态投影"):
        archive.finalize_cycle(
            cycle_id="c1", status="done", route="bootstrap",
            restored_sqlite_truth_only=True)
    assert archive.reconcile_sqlite(conn) == ["c1"]

    state = json.loads((
        tmp_path / "cycles" / "c1" / "cycle_state.json").read_text(
            encoding="utf-8"))
    assert state["restore_provenance"]["source_cycle"] == "c1"
    assert state["reasoning"]["availability"] == (
        "not_restored_sqlite_truth_only")
    assert state["reasoning"]["stage_event_id"] is None
    report = (tmp_path / "cycles" / "c1" / "cycle_report.md").read_text(
        encoding="utf-8")
    assert "SQLite-only 快照" in report
    assert "未发生" not in report
    closure = archive.verify_cycle("c1")
    assert closure["report_kind"] == "restored_sqlite_truth_only"
    assert closure["source_event_id"] is None
    conn.close()


def _seed_done_dag_cycle(conn, *, omit=None):
    """Arrange one complete A -> B DAG using only durable public facts."""
    conn.execute(
        "INSERT INTO goal(id,version,text,predicate_json) "
        "VALUES (1,1,'DAG replay','{}')")
    conn.execute(
        "INSERT INTO cycle("
        "id,goal_id,goal_ver,status,route,policy_version,next_intent"
        ") VALUES (1,1,1,'done','attack','p1','terminate')")
    conn.execute(
        "INSERT INTO question("
        "id,goal_id,goal_ver,born_goal_ver,text,status,source,born_cycle"
        ") VALUES (1,1,1,1,'DAG replay question','active','agent',1)")
    conn.execute("UPDATE cycle SET active_question_id=1 WHERE id=1")
    conn.execute(
        "INSERT INTO protocol(id,version,name,scope_spec_json) "
        "VALUES (1,1,'dag-replay','{}')")

    declarations = {
        11: {
            "target_key": "A", "target_kind": "build", "seq": 1,
            "critical": True, "budget_estimate": 1,
            "depends_on": [], "published_source_inputs": [],
            "resources": {"gpu_count": 0},
        },
        12: {
            "target_key": "B", "target_kind": "build", "seq": 2,
            "critical": True, "budget_estimate": 1,
            "depends_on": ["A"],
            "parent_baseline": {"target_key": "A"},
            "published_source_inputs": [
                {"input_key": "base", "target_key": "A"},
            ],
            "resources": {"gpu_count": 1},
        },
    }
    for target_id in (11, 12):
        baseline_id = 20 + (target_id - 11) * 10
        variant_id = baseline_id + 1
        evaluation_id = 101 + (target_id - 11)
        attempt_id = 201 + (target_id - 11)
        conn.execute(
            "INSERT INTO baseline("
            "id,slug,canonical_key,parent_id,born_cycle,status"
            ") VALUES (?,?,?,?,?, 'legal')",
            (
                baseline_id, f"dag-{target_id}", f"dag-{target_id}",
                (
                    20
                    if target_id == 12 and omit != "parent"
                    else None
                ),
                1,
            ))
        conn.execute(
            "INSERT INTO variant("
            "id,baseline_id,variant_key,config_json,born_question,status"
            ") VALUES (?,?,?,'{}',1,'legal')",
            (variant_id, baseline_id, f"v-{target_id}"))
        conn.execute(
            "INSERT INTO build_target("
            "id,cycle_id,question_id,target_kind,seq,critical,status,"
            "baseline_id,variant_id,eval_key,plan_ref"
            ") VALUES (?,1,1,'build',?,1,'pending',?,?,?,?)",
            (
                target_id, target_id - 10, baseline_id, variant_id,
                declarations[target_id]["target_key"],
                json.dumps(
                    declarations[target_id],
                    ensure_ascii=False, sort_keys=True),
            ))
        conn.execute(
            "INSERT INTO evaluation("
            "id,variant_id,protocol_id,protocol_ver,eval_key,source,status,"
            "created_cycle,build_target_id,target_set_hash"
            ") VALUES (?,?,1,1,?,'factory','created',1,?,'dag-set')",
            (
                evaluation_id, variant_id,
                declarations[target_id]["target_key"], target_id,
            ))
        conn.execute(
            "INSERT INTO evaluation_attempt("
            "id,evaluation_id,cycle_id,build_target_id,attempt_no,purpose,status"
            ") VALUES (?,?,1,?,1,'factory','success')",
            (attempt_id, evaluation_id, target_id))
        conn.execute(
            "UPDATE evaluation SET status='success',canonical_attempt_id=? "
            "WHERE id=?",
            (attempt_id, evaluation_id))
        conn.execute(
            "UPDATE build_target SET status='complete',evaluation_id=? "
            "WHERE id=?",
            (evaluation_id, target_id))

    if omit != "target":
        conn.execute(
            "INSERT INTO bundle_target_node("
            "target_id,cycle_id,target_key,parent_target_id"
            ") VALUES (11,1,'A',NULL)")
        conn.execute(
            "INSERT INTO bundle_target_node("
            "target_id,cycle_id,target_key,parent_target_id"
            ") VALUES (12,1,'B',11)")
    if omit not in {"dependency", "target"}:
        conn.execute(
            "INSERT INTO bundle_target_dependency("
            "cycle_id,upstream_target_id,downstream_target_id"
            ") VALUES (1,11,12)")
    conn.execute(
        "INSERT INTO bundle_resource_request("
        "build_target_id,cycle_id,gpu_count"
        ") VALUES (11,1,0)")
    conn.execute(
        "INSERT INTO bundle_resource_request("
        "build_target_id,cycle_id,gpu_count"
        ") VALUES (12,1,1)")
    request_id = None
    if omit not in {"input", "target"}:
        request_id = conn.execute(
            "INSERT INTO bundle_source_request("
            "cycle_id,downstream_target_id,input_key,upstream_target_id"
            ") VALUES (1,12,'base',11)").lastrowid

    admissions = {}
    for target_id in (11, 12):
        baseline_id = 20 + (target_id - 11) * 10
        variant_id = baseline_id + 1
        evaluation_id = 101 + (target_id - 11)
        attempt_id = 201 + (target_id - 11)
        phase_id = conn.execute(
            "INSERT INTO phase_commit("
            "cycle_id,stage,target_id,artifact_hash"
            ") VALUES (1,'bundle',?,?)",
            (target_id, f"bundle-{target_id}")).lastrowid
        manifest_hash = (f"{target_id:x}" * 64)[:64]
        manifest_ref = f"pool/manifests/{manifest_hash}.json"
        publication_id = conn.execute(
            "INSERT INTO decision("
            "cycle_id,question_id,actor,type,payload_json"
            ") VALUES (1,1,'gate','pool_publication',?)",
            (json.dumps({
                "schema": "meta-research-pool-db-binding/v1",
                "manifest_ref": manifest_ref,
                "manifest_hash": manifest_hash,
                "baseline_id": baseline_id,
                "variant_id": variant_id,
                "evaluation_id": evaluation_id,
                "attempt_id": attempt_id,
            }, ensure_ascii=False, sort_keys=True),)).lastrowid
        if omit != "admission" or target_id != 12:
            admission_id = conn.execute(
                "INSERT INTO bundle_target_admission("
                "target_id,cycle_id,phase_commit_id,publication_decision_id,"
                "manifest_ref,manifest_hash,baseline_id,variant_id,"
                "evaluation_id,attempt_id,source_ref,source_hash,"
                "source_hash_alg"
                ") VALUES (?,1,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    target_id, phase_id, publication_id,
                    manifest_ref, manifest_hash, baseline_id, variant_id,
                    evaluation_id, attempt_id,
                    f"pool/baselines/dag-{target_id}/source",
                    ("a" if target_id == 11 else "b") * 64,
                    "sha256-tree-v1",
                )).lastrowid
            admissions[target_id] = {
                "id": int(admission_id),
                "publication_id": int(publication_id),
                "manifest_ref": manifest_ref,
                "manifest_hash": manifest_hash,
            }
        conn.execute(
            "INSERT INTO decision("
            "cycle_id,question_id,actor,type,payload_json"
            ") VALUES (1,1,'orchestrator','bundle_scientific_contract',?)",
            (json.dumps({
                "protocol": "bundle-scientific-contract-v1",
                "build_target_id": target_id,
                "execution_status": "succeeded",
                "validity_status": "valid",
                "scientific_outcome": "supported",
                "pool_eligibility": "eligible",
            }, ensure_ascii=False, sort_keys=True),))

        for review_kind in ("bundle_code_review", "bundle_result_review"):
            if omit == "review" and target_id == 12 and (
                    review_kind == "bundle_result_review"):
                continue
            conn.execute(
                "INSERT INTO decision("
                "cycle_id,question_id,actor,type,payload_json"
                ") VALUES (1,1,'judge',?,?)",
                (review_kind, json.dumps({
                    "build_target_id": target_id,
                    "review_kind": review_kind,
                    "verdict": "pass",
                    "subject_hash": f"subject-{target_id}-{review_kind}",
                }, ensure_ascii=False, sort_keys=True)))

        for role in ("worker", "code_review", "result_review"):
            if omit == "worker" and target_id == 12 and role == "worker":
                continue
            conn.execute(
                "INSERT INTO bundle_worker_task("
                "build_target_id,cycle_id,role,provider_task_id,status,receipt_ref"
                ") VALUES (?,1,?,?, 'completed',?)",
                (
                    target_id, role, f"task-{target_id}-{role}",
                    f"receipts/{target_id}-{role}.json",
                ))

        if omit != "terminal" or target_id != 12:
            conn.execute(
                "INSERT INTO bundle_terminal_report("
                "build_target_id,cycle_id,report_ref,report_hash,status,"
                "summary_json"
                ") VALUES (?,1,?,?,'complete',?)",
                (
                    target_id, f"reports/target-{target_id}.json",
                    ("c" if target_id == 11 else "d") * 64,
                    json.dumps({
                        "protocol": "bundle-target-terminal-report-v1",
                        "target_kind": "build",
                        "seq": target_id - 10,
                        "critical": True,
                        "failure_kind": None,
                        "metric_result_ids": [],
                        "admitted": True,
                    }, ensure_ascii=False, sort_keys=True),
                ))

    assert request_id is not None or omit in {"input", "target"}
    if request_id is not None:
        upstream = admissions[11]
        conn.execute(
            "INSERT INTO bundle_source_binding("
            "request_id,cycle_id,downstream_target_id,input_key,"
            "upstream_target_id,upstream_admission_id,"
            "publication_decision_id,manifest_ref,manifest_hash,"
            "source_ref,source_hash,source_hash_alg"
            ") VALUES (?,1,12,'base',11,?,?,?,?,?,?,?)",
            (
                request_id, upstream["id"], upstream["publication_id"],
                upstream["manifest_ref"], upstream["manifest_hash"],
                "pool/baselines/dag-11/source", "a" * 64,
                "sha256-tree-v1",
            ))
    if omit != "lease":
        conn.execute(
            "INSERT INTO bundle_resource_lease("
            "build_target_id,cycle_id,resource_kind,resource_key,"
            "contract_hash,status,released_at,guardian_receipt_ref"
            ") VALUES (12,1,'gpu','GPU-a',?,'released',"
            "'2026-07-26T00:01:00Z','receipts/target-12-drained.json')",
            ("sha256:" + "e" * 64,))
    conn.execute(
        "INSERT INTO phase_commit(cycle_id,stage,target_id,artifact_hash) "
        "VALUES (1,'reasoning',NULL,'reasoning-dag')")
    conn.commit()


def _seed_done_descendant_skip_cycle(
        conn, *, skip_payload=None, critical_failure=False):
    """Arrange A failed -> B skipped before any B Worker dispatch."""
    conn.execute(
        "INSERT INTO goal(id,version,text,predicate_json) "
        "VALUES (1,1,'DAG skip replay','{}')")
    conn.execute(
        "INSERT INTO cycle("
        "id,goal_id,goal_ver,status,route,policy_version,next_intent"
        ") VALUES (1,1,1,'done','attack','p1','terminate')")
    conn.execute(
        "INSERT INTO question("
        "id,goal_id,goal_ver,born_goal_ver,text,status,source,born_cycle"
        ") VALUES (1,1,1,1,'DAG skip question','active','agent',1)")
    conn.execute("UPDATE cycle SET active_question_id=1 WHERE id=1")

    declarations = {
        11: {
            "target_key": "A", "target_kind": "build", "seq": 1,
            "critical": critical_failure, "budget_estimate": 1,
            "depends_on": [], "published_source_inputs": [],
            "resources": {"gpu_count": 0},
        },
        12: {
            "target_key": "B", "target_kind": "build", "seq": 2,
            "critical": True, "budget_estimate": 1,
            "depends_on": ["A"],
            "parent_baseline": {"target_key": "A"},
            "published_source_inputs": [
                {"input_key": "base", "target_key": "A"},
            ],
            "resources": {"gpu_count": 1},
        },
    }
    targets = (
        (
            11, 20, 21, int(critical_failure), "failed",
            "artifact_invalid", "build_failed",
        ),
        (12, 30, 31, 1, "skipped", None, "abandoned"),
    )
    for (target_id, baseline_id, variant_id, critical, status,
         failure_kind, domain_status) in targets:
        conn.execute(
            "INSERT INTO baseline("
            "id,slug,canonical_key,parent_id,born_cycle,status"
            ") VALUES (?,?,?,?,1,?)",
            (
                baseline_id, f"skip-{target_id}", f"skip-{target_id}",
                20 if target_id == 12 else None, domain_status,
            ))
        conn.execute(
            "INSERT INTO variant("
            "id,baseline_id,variant_key,config_json,born_question,status"
            ") VALUES (?,?,?,'{}',1,?)",
            (
                variant_id, baseline_id, f"v-{target_id}",
                domain_status,
            ))
        conn.execute(
            "INSERT INTO build_target("
            "id,cycle_id,question_id,target_kind,seq,critical,status,"
            "failure_kind,baseline_id,variant_id,eval_key,plan_ref"
            ") VALUES (?,1,1,'build',?,?,?,?,?,?,?,?)",
            (
                target_id, target_id - 10, critical, status, failure_kind,
                baseline_id, variant_id,
                declarations[target_id]["target_key"],
                json.dumps(
                    declarations[target_id],
                    ensure_ascii=False, sort_keys=True),
            ))
        conn.execute(
            "INSERT INTO bundle_target_node("
            "target_id,cycle_id,target_key,parent_target_id"
            ") VALUES (?,1,?,?)",
            (
                target_id, declarations[target_id]["target_key"],
                11 if target_id == 12 else None,
            ))
        conn.execute(
            "INSERT INTO bundle_resource_request("
            "build_target_id,cycle_id,gpu_count"
            ") VALUES (?,1,?)",
            (target_id, declarations[target_id]["resources"]["gpu_count"]))
        conn.execute(
            "INSERT INTO phase_commit("
            "cycle_id,stage,target_id,artifact_hash"
            ") VALUES (1,'bundle',?,?)",
            (target_id, f"bundle-{target_id}"))

        execution_status = "failed" if target_id == 11 else "skipped"
        conn.execute(
            "INSERT INTO decision("
            "cycle_id,question_id,actor,type,payload_json"
            ") VALUES (1,1,'orchestrator','bundle_scientific_terminal',?)",
            (json.dumps({
                "protocol": "bundle-scientific-terminal-v1",
                "build_target_id": target_id,
                "target_status": status,
                "failure_kind": failure_kind,
                "contract_hash": ("a" if target_id == 11 else "b") * 64,
                "execution_status": execution_status,
                "validity_status": "not_assessed",
                "scientific_outcome": "unavailable",
                "pool_eligibility": "ineligible",
            }, ensure_ascii=False, sort_keys=True),))
        report_status = "failed" if target_id == 11 else "skipped"
        conn.execute(
            "INSERT INTO bundle_terminal_report("
            "build_target_id,cycle_id,report_ref,report_hash,status,"
            "summary_json"
            ") VALUES (?,1,?,?,?,?)",
            (
                target_id, f"reports/target-{target_id}.json",
                ("c" if target_id == 11 else "d") * 64,
                report_status,
                json.dumps({
                    "protocol": "bundle-target-terminal-report-v1",
                    "target_kind": "build",
                    "seq": target_id - 10,
                    "critical": bool(critical),
                    "failure_kind": failure_kind,
                    "metric_result_ids": [],
                    "admitted": False,
                }, ensure_ascii=False, sort_keys=True),
            ))

    conn.execute(
        "INSERT INTO bundle_target_dependency("
        "cycle_id,upstream_target_id,downstream_target_id"
        ") VALUES (1,11,12)")
    conn.execute(
        "INSERT INTO bundle_source_request("
        "cycle_id,downstream_target_id,input_key,upstream_target_id"
        ") VALUES (1,12,'base',11)")
    conn.execute(
        "INSERT INTO bundle_worker_task("
        "build_target_id,cycle_id,role,provider_task_id,status,receipt_ref"
        ") VALUES (11,1,'worker','task-11-worker','completed',"
        "'receipts/11-worker.json')")
    default_skip_payload = {
        "failed_target_id": 11,
        "failure_status": "failed",
        "propagation": (
            "critical_drain" if critical_failure else "descendants"),
        "skipped_target_ids": [12],
    }
    conn.execute(
        "INSERT INTO decision("
        "cycle_id,actor,type,payload_json"
        ") VALUES (1,'orchestrator',?,?)",
        (
            (
                "bundle_critical_early_exit"
                if critical_failure else "bundle_descendant_skip"
            ),
            json.dumps(
                (
                    default_skip_payload
                    if skip_payload is None else skip_payload
                ),
                ensure_ascii=False, sort_keys=True),
        ))
    conn.execute(
        "INSERT INTO phase_commit(cycle_id,stage,target_id,artifact_hash) "
        "VALUES (1,'reasoning',NULL,'reasoning-dag-skip')")
    conn.commit()


def test_done_dag_replay_accepts_proven_never_dispatched_descendant_skip(
        tmp_path):
    conn = db.connect(str(tmp_path / "research.sqlite"))
    _seed_done_descendant_skip_cycle(conn)
    archive = CycleReplayArchive(tmp_path)
    archive.persist_stage_artifact(
        cycle_id="c1", stage="reasoning", artifact=_reasoning(),
        purpose="reasoning-main-c1", pack_hash="a" * 64)

    assert archive.reconcile_sqlite(conn) == ["c1"]

    state = json.loads((
        tmp_path / "cycles" / "c1" / "cycle_state.json").read_text(
            encoding="utf-8"))
    dag = state["bundle_dag"]
    request = dag["source_requests"][0]
    resources = {
        row["target_id"]: row for row in dag["resource_requests"]
    }
    assert request["downstream_target_id"] == 12
    assert request["binding"] is None
    assert resources[12]["gpu_count"] == 1
    assert resources[12]["leases"] == []
    assert {
        row["target_id"] for row in dag["worker_tasks"]
    } == {11}
    assert dag["worker_dispatches"] == []
    assert dag["skip_decisions"][0]["payload"] == {
        "failed_target_id": 11,
        "failure_status": "failed",
        "propagation": "descendants",
        "skipped_target_ids": [12],
    }
    conn.close()


def test_done_dag_replay_rejects_inexact_descendant_skip_decision(tmp_path):
    conn = db.connect(str(tmp_path / "research.sqlite"))
    _seed_done_descendant_skip_cycle(conn, skip_payload={
        "failed_target_id": 11,
        "failure_status": "failed",
        "propagation": "descendants",
        "skipped_target_ids": [],
    })
    archive = CycleReplayArchive(tmp_path)
    archive.persist_stage_artifact(
        cycle_id="c1", stage="reasoning", artifact=_reasoning(),
        purpose="reasoning-main-c1", pack_hash="a" * 64)

    with pytest.raises(
            CycleReplayError, match="descendant skip graph closure"):
        archive.reconcile_sqlite(conn)

    assert not (
        tmp_path / "cycles" / "c1" / "cycle_state.json").exists()
    conn.close()


def test_done_dag_replay_rejects_skip_with_worker_dispatch_evidence(tmp_path):
    conn = db.connect(str(tmp_path / "research.sqlite"))
    _seed_done_descendant_skip_cycle(conn)
    conn.execute(
        "INSERT INTO runner_call(cycle_id,phase,purpose,status) "
        "VALUES (1,'bundle','bundle-worker-c1-t12','aborted')")
    conn.commit()
    archive = CycleReplayArchive(tmp_path)
    archive.persist_stage_artifact(
        cycle_id="c1", stage="reasoning", artifact=_reasoning(),
        purpose="reasoning-main-c1", pack_hash="a" * 64)

    with pytest.raises(CycleReplayError, match="source input binding"):
        archive.reconcile_sqlite(conn)

    assert not (
        tmp_path / "cycles" / "c1" / "cycle_state.json").exists()
    conn.close()


def test_done_dag_replay_does_not_exempt_critical_drain_skip(tmp_path):
    conn = db.connect(str(tmp_path / "research.sqlite"))
    _seed_done_descendant_skip_cycle(conn, critical_failure=True)
    archive = CycleReplayArchive(tmp_path)
    archive.persist_stage_artifact(
        cycle_id="c1", stage="reasoning", artifact=_reasoning(),
        purpose="reasoning-main-c1", pack_hash="a" * 64)

    with pytest.raises(CycleReplayError, match="source input binding"):
        archive.reconcile_sqlite(conn)

    assert not (
        tmp_path / "cycles" / "c1" / "cycle_state.json").exists()
    conn.close()


def test_done_dag_replay_enumerates_complete_compact_closure(tmp_path):
    conn = db.connect(str(tmp_path / "research.sqlite"))
    _seed_done_dag_cycle(conn)
    archive = CycleReplayArchive(tmp_path)
    archive.persist_stage_artifact(
        cycle_id="c1", stage="reasoning", artifact=_reasoning(),
        purpose="reasoning-main-c1", pack_hash="a" * 64)

    assert archive.reconcile_sqlite(conn) == ["c1"]

    state = json.loads((
        tmp_path / "cycles" / "c1" / "cycle_state.json").read_text(
            encoding="utf-8"))
    dag = state["bundle_dag"]
    assert [row["target_key"] for row in dag["nodes"]] == ["A", "B"]
    assert [(row["upstream_target_id"], row["downstream_target_id"])
            for row in dag["dependencies"]] == [(11, 12)]
    assert dag["source_requests"][0]["binding"]["upstream_target_id"] == 11
    assert {
        row["target_id"]: row["gpu_count"]
        for row in dag["resource_requests"]
    } == {11: 0, 12: 1}
    assert dag["resource_requests"][1]["leases"][0]["status"] == "released"
    assert len(dag["admissions"]) == 2
    assert len(dag["terminal_reports"]) == 2
    assert all(
        "stdout" not in json.dumps(row, ensure_ascii=False).lower()
        for row in dag["terminal_reports"])
    conn.close()


@pytest.mark.parametrize(
    ("omitted_fact", "message"),
    [
        ("target", "target nodes"),
        ("dependency", "dependency edges"),
        ("input", "source input requests"),
        ("worker", "Worker task"),
        ("parent", "领域 baseline parent"),
        ("admission", "exact admission"),
        ("lease", "GPU lease"),
        ("review", "code/result review"),
        ("terminal", "required terminal reports"),
    ],
)
def test_done_dag_replay_fails_closed_when_required_closure_fact_is_missing(
        tmp_path, omitted_fact, message):
    conn = db.connect(str(tmp_path / f"{omitted_fact}.sqlite"))
    _seed_done_dag_cycle(conn, omit=omitted_fact)
    archive = CycleReplayArchive(tmp_path)
    archive.persist_stage_artifact(
        cycle_id="c1", stage="reasoning", artifact=_reasoning(),
        purpose="reasoning-main-c1", pack_hash="a" * 64)

    with pytest.raises(CycleReplayError, match=message):
        archive.reconcile_sqlite(conn)

    assert not (
        tmp_path / "cycles" / "c1" / "cycle_state.json").exists()
    conn.close()


def test_build_system_sqlite_path_seals_reasoning_report_before_snapshot(tmp_path):
    files = {
        "tree_ops.json": {"ops": [{
            "op": "create_root", "local_key": "root", "text": "首个可证据关闭的问题",
        }]},
        "selection.json": {
            "next_question_id": None, "next_intent": "terminate",
            "terminate_reason_md": "集成测试收口",
        },
    }
    report = "# 本轮报告\n\nBootstrap 已提出首题；本轮按测试合同终止。\n"

    class Runner:
        def run_task(self, *, system_prompt, skill, context_pack):
            return Artifact(
                stage=context_pack.stage, files=files, md=report,
                usage=CallUsage(tokens_known=True))

    system_root = str(Path(__file__).resolve().parent.parent)
    system = build_system(
        system_root, str(tmp_path), runner_factory=lambda _td, _purpose: Runner(),
        attack=False)
    try:
        assert system.run(1) == ["c1"]
        cycle = tmp_path / "cycles" / "c1"
        assert (cycle / "cycle_report.md").read_text(encoding="utf-8") == report
        assert (cycle / "context_pack" / "reasoning.pack.json").is_file()
        assert (cycle / "artifacts" / "selection.json").is_file()
        assert (cycle / "handoff-1.md").is_file()
        assert (tmp_path / "state" / "storage" / "cycles" / "c1.json").is_file()
        assert CycleReplayArchive(tmp_path).verify_cycle("c1")["coverage"]["legacy_incomplete"] is False
    finally:
        system.close()
