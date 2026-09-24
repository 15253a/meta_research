"""Large research data crosses real Bundle, Target, RM and RG Owner seams."""
from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

from sqlalchemy import text

from meta_research.owners.common import canonical_json
from meta_research.owners.target_run_runtime import canonical_target_scope_binding
from meta_research.target_commit_evidence import TargetCommitEvidenceCatalog
from meta_research.target_run_finalizer import TargetRunFinalizer
from meta_research.target_run_runtime_contract import TargetCompletionArtifact
from test_public_bundle_stage import _TwoTargetBundleSkill
from test_report_only_evaluation import _report_protocol, _report_work
from test_target_root_finalizer import _CurrentBindingBundleSkill, _EvidenceReader, _root_finalizer_fixture


def _large_file(path: Path, marker: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as output:
        output.seek(65 * 1024 * 1024 - 1)
        output.write(marker)
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def test_bundle_completed_large_dataset_and_report_feed_dependent_target(tmp_path, monkeypatch):
    # Deterministic stage output stands in for the model only. Every business
    # receipt, authorization, launch, commit and input binding is Owner-issued.
    def sequential_plan(_self, request):
        return _TwoTargetBundleSkill()._target_plan(request)
    monkeypatch.setattr(_CurrentBindingBundleSkill, "_target_plan", sequential_plan)
    _report_protocol(monkeypatch)
    runtime, lifecycle, memory, authority, handle, workspace, evidence = _root_finalizer_fixture(tmp_path)
    try:
        graph_owner = runtime.owners.research_graph
        graph = graph_owner.query_target_graph(authority.stage_request_ref)
        assert len(graph.targets) == 2
        dependent = next(target for target in graph.targets if target.target_ref != handle.target_ref)
        assert runtime.owners.agent_runtime.query_admitted_target_launch(dependent.target_ref) is None
        assert authority.measurement_contract.protocol_version.required_metric_keys == ()

        data_path = "outputs/data/collected"
        checkpoint_path = "outputs/checkpoints/collection-state.bin"
        data_hash = _large_file(workspace / data_path / "observations.bin", b"d")
        checkpoint_hash = _large_file(workspace / checkpoint_path, b"c")
        (workspace / data_path / "empty").mkdir()
        evidence, report_path = _report_work(workspace, evidence,
            artifact_paths=["outputs/analysis/evaluation/report.md"])
        evidence = replace(evidence, handoff=replace(evidence.handoff, artifacts=(
            *evidence.handoff.artifacts,
            TargetCompletionArtifact(role="data", relative_path=data_path),
            TargetCompletionArtifact(role="checkpoint", relative_path=checkpoint_path),
        )))
        result_path = workspace / "outputs/metrics.json"
        document = json.loads(result_path.read_text())
        run = document["formal_runs"][0]
        run["artifact_paths"] = [data_path]
        run["checkpoint_paths"] = [artifact.relative_path for artifact in evidence.handoff.artifacts
                                   if artifact.role == "checkpoint"]
        run["evaluations"][0]["checkpoint_paths"] = [checkpoint_path]
        result_path.write_text(canonical_json(document), encoding="utf-8")

        finalizer = TargetRunFinalizer(lifecycle=lifecycle, memory=memory,
            workspace_resolver=runtime.target_run_authorities.agent_runtime,
            evidence_reader=_EvidenceReader(evidence), measurement_authority=graph_owner,
            graph_authority=graph_owner)
        completed = finalizer.finalize(handle=handle, evidence=evidence)
        assert completed.status == "completed", completed
        published = runtime.owners.agent_runtime.publish_target_root_completion(
            target_ref=handle.target_ref, completion_ref=completed.completion_ref,
            target_commit_ref=completed.target_commit_ref)
        assert published.terminal is not None
        lifecycle.mark_completed(target_ref=handle.target_ref, completion_ref=completed.completion_ref)
        assert lifecycle.query(handle.target_ref).status == "completed"
        manifest = memory.query(completed.manifest_ref)
        data = next(entry for entry in manifest.entries if entry.declared_relative_path == data_path)
        checkpoint = next(entry for entry in manifest.entries if entry.declared_relative_path == checkpoint_path)
        report = next(entry for entry in manifest.entries if entry.declared_relative_path == report_path)
        assert data.artifact_kind == "directory" and data.media_type == "application/x-directory"
        assert data.byte_count == checkpoint.byte_count == 65 * 1024 * 1024
        assert checkpoint.content_hash == checkpoint_hash
        fact, = graph_owner.query_target_formal_results(handle.target_ref)
        assert fact["variant_run"]["status"] == "executed"
        assert fact["evaluation_attempt"]["status"] == "measurement_accepted"
        assert fact["metric_result"]["metrics"] == {}
        assert [(item["role"], item["version_ref"]) for item in fact["run_artifacts"]
                if item["role"] == "data_asset"] == [
            ("data_asset", data.binding.version_ref)]
        assert checkpoint.binding.version_ref in {item["version_ref"] for item in fact["run_artifacts"]
                                                  if item["role"] == "checkpoint_artifact"}
        assert report.binding.version_ref in {item["version_ref"] for item in fact["evaluation_artifacts"]}
        assert data.binding.version_ref not in {item["version_ref"] for item in fact["evaluation_artifacts"]}
        assert runtime.owners.research_memory.materialize_asset(report.binding.version_ref).content.startswith(b"# Material inspection")
        dataset = graph_owner.register_dataset(semantic_key="workflow:collected-observations",
            name="Collected observations", meaning="The retained collection produced by this execution.",
            idempotency_key="workflow-dataset")
        dataset_version = graph_owner.register_dataset_version(dataset_ref=dataset["dataset_ref"],
            version_label="collection-v1", meaning="Exact retained collection including its empty partition.",
            asset_bindings=[data.binding], idempotency_key="workflow-dataset-version")
        question, = graph_owner.query_question_tree(quest_ref=graph.quest_ref)
        reference = graph_owner.reference_dataset(dataset_version_ref=dataset_version["dataset_version_ref"],
            question_ref=question.question_ref, purpose="Data produced by the collection execution",
            research_ref=fact["variant_run_ref"], idempotency_key="workflow-dataset-production")
        assert reference["research_ref"] == fact["variant_run_ref"]
        assert graph_owner.query_dataset_version(dataset_version["dataset_version_ref"])["asset_bindings"] == [data.binding.as_dict()]
        assert finalizer.finalize(handle=handle, evidence=evidence) == completed

        commit, = graph_owner.query_target_commits(graph.graph_ref)
        runtime.bundle_stage._publish_target_commit_evidence(quest_ref=graph.quest_ref, commit=commit)
        leaves = TargetCommitEvidenceCatalog(graph_owner, runtime.owners.research_memory).resolve_reasoning_target_evidence_leaves(
            quest_ref=graph.quest_ref, target_commit_refs=(completed.target_commit_ref,))
        assert len(leaves) == 1 and leaves[0].evidence_item_ref == fact["metric_result_ref"]
        # Bundle now consumes the real Commit and admits the dependent Target.
        for _step in range(16):
            runtime.bundle_stage.process_once()
            launch = runtime.owners.agent_runtime.query_admitted_target_launch(dependent.target_ref)
            if launch is not None:
                break
        assert launch is not None
        assert launch.request.accepted_input_target_commit_refs == (completed.target_commit_ref,)

        candidate = graph_owner.query_target_candidate_projection(target_ref=dependent.target_ref).candidate
        formal_plan = graph_owner.query_target_formal_plan_projection(graph_ref=graph.graph_ref).formal_plan
        scope = canonical_target_scope_binding(target_ref=dependent.target_ref,
            target_run_ref=launch.target_run_ref, target_spec_hash=launch.request.target_spec_binding.content_hash_ref,
            candidate=candidate, formal_plan=formal_plan,
            accepted_input_refs=tuple(sorted((*launch.request.accepted_input_target_commit_refs,
                                              *launch.request.accepted_input_asset_refs))))
        runtime.harnesses.admit_target_run(target_ref=dependent.target_ref, target_run_ref=launch.target_run_ref,
            harness_family="codex", model_ref="gpt-target-run", auth_profile_ref="harness-profile:codex-default",
            target_scope_binding=scope)
        for _step in range(6):
            runtime.target_run_runtime.process_once(dependent.target_ref)
            status = runtime.target_run_runtime.query_status(dependent.target_ref)
            if status is not None and status.phase == "root_activated":
                break
        assert status.phase == "root_activated"
        next_handle = runtime.target_run_authorities.agent_runtime.query_current_target_work_handle(dependent.target_ref)
        assert next_handle.accepted_input_target_commit_refs == (completed.target_commit_ref,)
        inputs = runtime.target_run_finalizer.materialize_inputs(handle=next_handle)
        input_manifest_path = Path(inputs[0])
        input_manifest = json.loads(input_manifest_path.read_text())
        upstream = next(item for item in input_manifest["entries"] if item["kind"] == "target_commit")
        data_entry = next(item for item in upstream["artifacts"] if item["version_ref"] == data.binding.version_ref)
        delivered = input_manifest_path.parent / data_entry["relative_path"]
        assert delivered.is_dir() and (delivered / "empty").is_dir()
        with (delivered / "observations.bin").open("rb") as source:
            assert hashlib.file_digest(source, "sha256").hexdigest() == data_hash
        assert (delivered / "observations.bin").stat().st_mode & 0o222 == 0
        assert not data_entry["relative_path"].endswith(".zip")
        assert len(input_manifest_path.read_bytes()) < 64 * 1024
        with runtime._database.read() as connection:
            assert connection.execute(text("SELECT COUNT(*) FROM ar_target_launches")).scalar_one() == 2
            assert connection.execute(text("SELECT COUNT(*) FROM rg_target_commits")).scalar_one() == 1
            assert connection.execute(text(
                "SELECT COUNT(*) FROM rg_evaluation_attempt_checkpoints c "
                "JOIN rg_experiment_asset_roles r ON r.role_ref=c.checkpoint_role_ref "
                "WHERE c.evaluation_attempt_ref=:attempt AND r.version_ref=:version"
            ), {"attempt": fact["evaluation_attempt_ref"], "version": checkpoint.binding.version_ref}).scalar_one() == 1
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall() == []
    finally:
        runtime.close()
