"""Same-root research explanations retain exact versions through finalization."""

import errno
import hashlib
import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

import meta_research.owners.research_memory as memory_module
import meta_research.target_run_finalizer as finalizer_module
from meta_research.owners.common import OwnerConflict, canonical_json
from meta_research.research_notes import manifest_research_notes, read_note_body
from meta_research.semantic_mcp import SemanticMcpGateway
from meta_research.stage_context_access import stage_context_operations
from meta_research.target_run_finalizer import TargetRunFinalizer
from test_asset_intake_resume_recovery import _resume
from test_research_notes_and_call_observations import _SystemEvidenceReader
from test_target_launch_admission import _ready_launch
from test_target_root_finalizer import _current_bundle_runtime, _root_finalizer_fixture


@pytest.mark.parametrize("explanation_changes,failure_kind", [
    (True, None), (False, None), (True, "storage"), (True, "host-request"),
    (True, "baseline-host-request"), (True, "baseline-invalid-predecessor"),
], ids=["revised", "unchanged", "storage-recovery", "host-request-recovery",
        "baseline-host-request-recovery", "baseline-unrelated-failure-blocked"])
def test_same_root_note_revision_reaches_target_commit_and_bundle_exact_reader(
    tmp_path, monkeypatch, explanation_changes, failure_kind,
):
    baseline_failure = failure_kind in {"baseline-host-request", "baseline-invalid-predecessor"}
    if baseline_failure:
        monkeypatch.setattr(finalizer_module, "TARGET_ROOT_INLINE_ARTIFACT_BYTES", 1)
    runtime = _current_bundle_runtime(tmp_path / "note-revision")
    try:
        ready = _ready_launch(runtime)
        graph, _, bundle_run, _, _ = ready
        _, lifecycle, memory, _, handle, workspace, old = _root_finalizer_fixture(
            tmp_path, runtime=runtime, ready=ready,
        )
        # The production composition binds actual AR/RG/control recovery readers.
        memory = runtime.target_run_finalizer._memory
        result_path = workspace / "outputs/result.json"
        document = json.loads((workspace / "outputs/result.json").read_text(encoding="utf-8"))
        document["formal_runs"] = [{
            "run_key": "existing-work", "input_refs": ["not-an-admitted-input"],
            "artifact_paths": ["logs"], "checkpoint_paths": [],
            "evaluations": [{"attempt_key": "existing-assessment", "metrics": document["metrics"]}],
        }]
        result_path.write_text(canonical_json(document), encoding="utf-8")
        note_path = workspace / "outputs/analysis/research-note.md"
        note_path.parent.mkdir()
        first_body = "The measured effect is uncertain; independent validation is missing.\n"
        first_statement = "The recorded assessment is complete, with an unresolved evidence boundary."
        note_path.write_text(first_body, encoding="utf-8")
        workspace_ref, _ = runtime.target_run_authorities.agent_runtime.resolve_target_workspace(
            target_ref=handle.target_ref, target_run_ref=handle.target_run_ref,
            root_session_ref=handle.root_session_ref, attempt_ref=handle.execution_attempt_ref,
            fence_ref=handle.execution_fence_ref,
        )
        evidence = replace(old, workspace_ref=workspace_ref,
            final_text=first_statement,
            final_text_sha256=hashlib.sha256(first_statement.encode()).hexdigest())
        finalizer = TargetRunFinalizer(lifecycle=lifecycle, memory=memory,
            workspace_resolver=runtime.target_run_authorities.agent_runtime,
            evidence_reader=_SystemEvidenceReader(), measurement_authority=runtime.owners.research_graph,
            graph_authority=runtime.owners.research_graph)

        # RM retains the first explanation; the real RG returns a correctable
        # formal input error, allowing this native root to revise its handoff.
        rejected = finalizer.finalize(handle=handle, evidence=evidence)
        assert rejected.status == "revision_required"
        assert rejected.pending_code == "target_formal_input_reference_invalid"
        first_manifest = memory.query(rejected.manifest_ref)
        first_notes = {item["kind"]: item for item in manifest_research_notes(first_manifest)}
        first_completion = lifecycle.query_completion_by_ref(rejected.completion_ref)
        assert read_note_body(runtime.owners.research_memory, first_notes["research_note"]) == first_body
        assert read_note_body(runtime.owners.research_memory, first_notes["final_statement"]) == first_statement
        assert finalizer.finalize(handle=handle, evidence=evidence) == rejected
        unchanged_evidence = replace(evidence, operation_ref="unchanged-rg-rejection-retry",
            evidence_ref="unchanged-rg-rejection-evidence", operation_generation=2,
            evidence_sequence=11, observed_at=1.5)
        with pytest.raises(OwnerConflict, match="^target_root_completion_successor_unchanged$"):
            finalizer.finalize(handle=handle, evidence=unchanged_evidence)

        revised_body = ("The input declaration is corrected; the same measured effect remains uncertain.\n"
                        if explanation_changes else first_body)
        revised_statement = ("The existing assessment is retained; independent validation is still missing."
                             if explanation_changes else first_statement)
        document["formal_runs"][0]["input_refs"] = []
        result_path.write_text(canonical_json(document), encoding="utf-8")
        note_path.write_text(revised_body, encoding="utf-8")
        successor = replace(evidence, operation_ref="same-root-note-revision", operation_generation=2,
            evidence_ref="same-root-note-revision-evidence", evidence_sequence=20, observed_at=2.0,
            final_text=revised_statement,
            final_text_sha256=hashlib.sha256(revised_statement.encode()).hexdigest())
        predecessor_completion = first_completion
        if failure_kind == "storage":
            original_replace = os.replace

            def exhausted_destination(source, destination, *args, **kwargs):
                if Path(destination).name == successor.final_text_sha256:
                    raise OSError(errno.ENOSPC, "isolated final-statement storage is full")
                return original_replace(source, destination, *args, **kwargs)

            with monkeypatch.context() as failure:
                failure.setattr(memory_module, "ASSET_INTAKE_MAX_ATTEMPTS", 1)
                failure.setattr(os, "replace", exhausted_destination)
                with pytest.raises(OwnerConflict, match="^target_root_artifact_intake_unavailable$"):
                    finalizer.finalize(handle=handle, evidence=successor)
                pending = lifecycle.query_completion(handle.target_ref)
                assert pending.generation == 2 and pending.handle == handle
                assert lifecycle.query(handle.target_ref).status == "finalizing"
                assert lifecycle.query_completion_rejection(pending.completion_ref) is None
                assert memory.query_for_completion(pending.completion_ref) is None
                retained_note = runtime.owners.research_memory.query_asset_lifecycle(
                    first_notes["research_note"]["asset_ref"])
                assert retained_note["revision"] == 2, "The revised note was retained before storage failed"
                with pytest.raises(OwnerConflict, match="^target_root_artifact_intake_unavailable$"):
                    finalizer.finalize(handle=handle, evidence=successor)
                assert lifecycle.query_completion(handle.target_ref) == pending
                assert runtime.owners.research_memory.query_asset_lifecycle(
                    first_notes["research_note"]["asset_ref"]) == retained_note
            _resume(runtime, "same-root-note-storage-recovery")
        elif failure_kind == "host-request" or baseline_failure:
            asset_memory = runtime.owners.research_memory
            original_submit = asset_memory.submit_asset_intake
            failed_intakes = []

            def malformed_revision_request(request, **values):
                malformed = request
                if request.asset_ref is not None:
                    if failure_kind == "baseline-invalid-predecessor":
                        malformed = replace(request, change={**request.change,
                            "predecessor_version_ref": "unaccepted-predecessor-version"})
                    else:
                        malformed = replace(request, change=None)
                result = original_submit(malformed, **values)
                if result.status == "failed":
                    failed_intakes.append(result)
                    if baseline_failure:
                        # Reproduce the baseline finalizer wrapper while keeping
                        # the actual RM intake fact and request unchanged.
                        return replace(result, failure_code="target_root_artifact_intake_failed")
                return result

            with monkeypatch.context() as failure:
                failure.setattr(asset_memory, "submit_asset_intake", malformed_revision_request)
                missing_change = finalizer.finalize(handle=handle, evidence=successor)
            assert missing_change.status == "revision_required"
            expected_code = ("target_root_artifact_intake_failed" if baseline_failure
                             else "asset_change_required")
            assert missing_change.pending_code == expected_code
            assert len(failed_intakes) == 1
            actual_code = ("asset_change_predecessor_invalid" if failure_kind == "baseline-invalid-predecessor"
                           else "asset_change_required")
            assert asset_memory.query_asset_intake(failed_intakes[0].job_ref).failure_code == actual_code
            feedback = missing_change.rejection_feedback.lower()
            if failure_kind == "host-request":
                assert "host" in feedback and "change" in feedback
                assert "resolve the actual storage problem" not in feedback
            predecessor_completion = lifecycle.query_completion_by_ref(missing_change.completion_ref)
            rejection = lifecycle.query_completion_rejection(missing_change.completion_ref)
            assert rejection.code == expected_code
            assert rejection.feedback == missing_change.rejection_feedback
            assert memory.query_for_completion(missing_change.completion_ref) is None
            assert lifecycle.query(handle.target_ref).status == "running"
            assert finalizer.finalize(handle=handle, evidence=successor) == missing_change
            # Once host construction is repaired, the original native root
            # resubmits the same scientific work; it does not rerun the assessment.
            successor = replace(successor, operation_ref="same-root-host-repaired", operation_generation=3,
                evidence_ref="same-root-host-repaired-evidence", evidence_sequence=30, observed_at=3.0)
            if failure_kind == "baseline-invalid-predecessor":
                with pytest.raises(OwnerConflict, match="^target_root_completion_successor_unchanged$"):
                    finalizer.finalize(handle=handle, evidence=successor)
                assert lifecycle.query_completion_rejection(missing_change.completion_ref) == rejection
                assert memory.query(first_manifest.manifest_ref) == first_manifest
                assert asset_memory.query_asset_lifecycle(first_notes["research_note"]["asset_ref"])["revision"] == 1
                return
        completed = finalizer.finalize(handle=handle, evidence=successor)
        assert completed.status == "completed", completed
        assert completed.completion_generation == (3 if failure_kind in {"host-request", "baseline-host-request"} else 2)
        if failure_kind in {"host-request", "baseline-host-request"}:
            assert lifecycle.query_completion_rejection(missing_change.completion_ref) == rejection
        if failure_kind == "storage":
            assert completed.completion_ref == pending.completion_ref
        completion = lifecycle.query_completion_by_ref(completed.completion_ref)
        assert completion.handle == first_completion.handle == handle
        assert completion.predecessor_completion_ref == predecessor_completion.completion_ref
        revised_manifest = memory.query(completed.manifest_ref)
        assert revised_manifest.implementation_tree_hash == first_manifest.implementation_tree_hash
        revised_notes = {item["kind"]: item for item in manifest_research_notes(revised_manifest)}
        for kind, old_body, new_body in (
            ("research_note", first_body, revised_body),
            ("final_statement", first_statement, revised_statement),
        ):
            previous, revised = first_notes[kind], revised_notes[kind]
            assert revised["asset_ref"] == previous["asset_ref"]
            assert (revised["version_ref"] != previous["version_ref"]) is explanation_changes
            assert read_note_body(runtime.owners.research_memory, previous) == old_body
            assert read_note_body(runtime.owners.research_memory, revised) == new_body
            history = runtime.owners.research_memory.query_asset_lifecycle(revised["asset_ref"])
            assert history["revision"] == (2 if explanation_changes else 1)
            assert history["current_version_ref"] == revised["version_ref"]
            if explanation_changes:
                assert history["changes"][-1]["predecessor_version_ref"] == previous["version_ref"]
        assert memory.query(first_manifest.manifest_ref) == first_manifest
        assert finalizer.finalize(handle=handle, evidence=successor) == completed

        commit = next(item for item in runtime.owners.research_graph.query_target_commits(graph.graph_ref)
                      if item.commit_ref == completed.target_commit_ref)
        assert commit.target_ref == handle.target_ref and commit.target_run_ref == handle.target_run_ref
        assert len(runtime.owners.research_graph.query_target_formal_results(handle.target_ref)) == 1
        assert commit.closure["research_notes"] == manifest_research_notes(revised_manifest)
        consumed = next(item for item in runtime.bundle_stage.query_current()["target_commits"]
                        if item["commit_ref"] == commit.commit_ref)
        assert consumed["closure"]["research_notes"] == commit.closure["research_notes"]

        # The Bundle's authenticated public note reader consumes exact old and
        # revised bodies, rather than silently substituting the current version.
        owners = runtime.owners
        gateway = SemanticMcpGateway(stage_context_operations(
            advancement_engine=owners.advancement_engine, agent_runtime=owners.agent_runtime,
            research_graph=owners.research_graph, research_memory=owners.research_memory))
        channel, _ = gateway.issue_channel(run_ref=bundle_run.run_ref, attempt_ref=bundle_run.attempt_ref,
            root_session_ref=bundle_run.root_session_ref, fence_ref=bundle_run.fence_ref,
            capability_binding_hash=bundle_run.runtime_binding_hash, root_kind="bundle",
            phase="target-batch", operation_ids=("research_memory.research_notes.read",))
        for notes, expected in ((first_notes, first_body), (revised_notes, revised_body)):
            status, response = gateway.dispatch(channel.token, {"jsonrpc": "2.0", "id": "exact-note",
                "method": "tools/call", "params": {"name": "research_memory.research_notes.read",
                "arguments": {"context_pack_ref": graph.context_pack_ref, "source": "research_note_body",
                    "source_ref": notes["research_note"]["version_ref"], "path": [], "offset": 0, "limit": 16384}}})
            assert status == 200 and response["result"]["isError"] is False, response
            body = json.loads(response["result"]["structuredContent"]["text"])
            assert body["body"] == expected
    finally:
        runtime.close()
