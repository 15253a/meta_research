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
        "cycle_report": True, "legacy_incomplete": False,
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
        "VALUES (1,1,1,'done','dependency_wait','p1','attack')")
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
