"""Current Target completion: real adapter, Harness, issuer and finalizer."""
import json

import pytest
from sqlalchemy import text

from meta_research.harness import HarnessAdmissionError, HarnessRuntime
from meta_research.harness_adapters import CodexHarnessAdapter, HarnessSupervisorTransport
from meta_research.owners.common import OwnerConflict, canonical_hash, canonical_json
from meta_research.research_notes import manifest_research_notes, read_note_body
from meta_research.target_run_finalizer import TargetRunFinalizer
from test_harness_stopped_profile_resume import _StoppedThenSuccessfulProvider, FINAL_TEXT
from test_target_root_finalizer import _root_finalizer_fixture, _current_bundle_runtime


def _pipeline(tmp_path, *, corrupt_receipt=False):
    runtime, lifecycle, memory, authority, handle, workspace, _unused = _root_finalizer_fixture(tmp_path)
    owner = runtime.owners.agent_runtime.harness_runs
    store = runtime.harnesses._target_raw_output_store
    class Provider(_StoppedThenSuccessfulProvider):
        def run_durable_job(self, *args, **kwargs):
            result = super().run_durable_job(*args, **kwargs)
            if corrupt_receipt:
                receipt = self.successful_request_paths[-1].with_name("supervisor-exit.json")
                receipt.write_bytes(receipt.read_bytes() + b"tampered")
            return result

    provider = Provider(stop_first=False)
    adapter = CodexHarnessAdapter(tmp_path / "actual-adapter", runner=HarnessSupervisorTransport(
        store._workspace, process_runner=provider,
        event_sink=owner.append_target_root_events, raw_output_store=store))
    harness = HarnessRuntime(owner, runtime.harnesses._gateway, (adapter,), target_raw_output_store=store)
    harness.bind_target_workspace_resolver(runtime.target_run_authorities.agent_runtime)
    request = owner.query_target_run_by_ref(handle.target_run_ref)
    finalizer = TargetRunFinalizer(lifecycle=lifecycle, memory=memory,
        workspace_resolver=runtime.target_run_authorities.agent_runtime,
        evidence_reader=harness, measurement_authority=runtime.owners.research_graph,
        graph_authority=runtime.owners.research_graph)
    if corrupt_receipt:
        with pytest.raises(HarnessAdmissionError):
            harness.run_or_resume_target_root(request.request_ref,
                prompt="Finish the retained Target work.", mcp_base_url="http://127.0.0.1:8999")
        assert harness.query_target_root_completion_evidence(handle.target_ref) is None
        evidence = _unused
    else:
        harness.run_or_resume_target_root(request.request_ref,
            prompt="Finish the retained Target work and explain its limitations.",
            mcp_base_url="http://127.0.0.1:8999")
        evidence = harness.query_target_root_completion_evidence(handle.target_ref)
        assert evidence is not None and evidence.final_text == FINAL_TEXT
        assert not hasattr(evidence, "handoff")
    return runtime, handle, provider, evidence, finalizer, memory


def test_system_text_reaches_assets_downstream_and_restarts_without_provider(tmp_path):
    runtime, handle, provider, evidence, finalizer, memory = _pipeline(tmp_path)
    try:
        completed = finalizer.finalize(handle=handle, evidence=evidence)
        assert completed.status == "completed"
        manifest = memory.query(completed.manifest_ref)
        notes = {entry["kind"]: entry for entry in manifest_research_notes(manifest)}
        assert read_note_body(runtime.owners.research_memory, notes["final_statement"]) == FINAL_TEXT
        assert runtime.owners.research_graph.query_target_formal_results(handle.target_ref)
        completion = runtime.target_root_lifecycle.query_completion(handle.target_ref)
        assert completion.handoff.result_document_path == "outputs/result.json"
        assert finalizer.finalize(handle=handle, evidence=evidence) == completed
        assert provider.calls == 1
    finally:
        runtime.close()
    reopened = _current_bundle_runtime(tmp_path / "target-root-finalizer")
    try:
        restored = reopened.harnesses.query_target_root_completion_evidence(handle.target_ref)
        assert restored == evidence
        replay = reopened.target_run_finalizer.finalize(handle=handle, evidence=restored)
        assert replay == completed and provider.calls == 1
        assert reopened.target_root_lifecycle.query_completion(handle.target_ref).handoff == completion.handoff
    finally:
        reopened.close()


@pytest.mark.parametrize("damage", ["missing_binding", "agent_envelope", "result_candidate", "text", "scope", "terminal"])
def test_real_finalizer_rejects_missing_or_tampered_system_binding(tmp_path, damage):
    runtime, handle, provider, evidence, finalizer, _memory = _pipeline(tmp_path)
    try:
        with runtime._database.write() as connection:
            rows = connection.execute(text("SELECT event_ref, summary_json FROM ar_harness_evidence_events WHERE operation_ref=:ref ORDER BY sequence"), {"ref": evidence.operation_ref}).all()
            row = next(row for row in rows if json.loads(row.summary_json).get(
                "target_root_terminal" if damage == "terminal" else "target_root_agent_message"))
            event = json.loads(row.summary_json)
            if damage in {"missing_binding", "agent_envelope", "result_candidate"}:
                event.pop("target_root_completion_binding")
                if damage in {"agent_envelope", "result_candidate"}:
                    candidate = {"artifacts": [{"role": "implementation", "relative_path": "implementation"},
                                               {"role": "result", "relative_path": "outputs/result.json"}],
                                 "result_document_path": "outputs/result.json", "summary": FINAL_TEXT}
                    if damage == "agent_envelope":
                        candidate.update(schema_ref="meta-research/target-completion-handoff/v1",
                                         status="completed", target_ref=handle.target_ref,
                                         target_run_ref=handle.target_run_ref)
                    key = "target_root_completion_candidate" if damage == "agent_envelope" else "target_root_completion_result_candidate"
                    event[key] = candidate
            elif damage == "text": event["target_root_completion_binding"]["final_text"] += "forged"
            elif damage == "scope": event["target_run_scope"]["fence_ref"] = "another-fence"
            else: event["target_root_terminal"] = False
            connection.execute(text("UPDATE ar_harness_evidence_events SET summary_json=:value, summary_hash=:hash WHERE event_ref=:ref"), {"value": canonical_json(event), "hash": canonical_hash(event), "ref": row.event_ref})
        with pytest.raises(OwnerConflict, match="target_root_completion_evidence_invalid"):
            finalizer.finalize(handle=handle, evidence=evidence)
        assert runtime.target_root_lifecycle.query_completion(handle.target_ref) is None
        assert provider.calls == 1
    finally:
        runtime.close()


def test_invalid_signed_exit_cannot_reach_completion_finalizer(tmp_path):
    runtime, handle, provider, evidence, finalizer, _memory = _pipeline(tmp_path, corrupt_receipt=True)
    try:
        with pytest.raises(OwnerConflict, match="target_root_completion_evidence_invalid"):
            finalizer.finalize(handle=handle, evidence=evidence)
        assert runtime.target_root_lifecycle.query_completion(handle.target_ref) is None
        assert provider.calls == 1
    finally:
        runtime.close()


def test_real_provider_revision_reuses_root_and_accepts_corrected_candidate(tmp_path):
    runtime, handle, provider, evidence, finalizer, memory = _pipeline(tmp_path)
    try:
        _ref, workspace = runtime.target_run_authorities.agent_runtime.resolve_target_workspace(
            target_ref=handle.target_ref, target_run_ref=handle.target_run_ref,
            root_session_ref=handle.root_session_ref, attempt_ref=handle.execution_attempt_ref,
            fence_ref=handle.execution_fence_ref)
        result_path = workspace / "outputs/result.json"
        original = result_path.read_bytes()
        result_path.write_text("{invalid", encoding="utf-8")
        rejected = finalizer.finalize(handle=handle, evidence=evidence)
        assert rejected.status == "revision_required"
        assert rejected.pending_code == "target_root_result_document_invalid"
        assert rejected.manifest_ref is None
        assert finalizer.finalize(handle=handle, evidence=evidence) == rejected
        assert provider.calls == 1
        rejected_completion = runtime.target_root_lifecycle.query_completion(handle.target_ref)
        result_path.write_bytes(original)
        harness = finalizer._evidence_reader
        request = runtime.owners.agent_runtime.harness_runs.query_target_run_by_ref(handle.target_run_ref)
        harness.run_or_resume_target_root(request.request_ref, prompt=rejected.rejection_feedback,
            mcp_base_url="http://127.0.0.1:8999")
        successor = harness.query_target_root_completion_evidence(handle.target_ref)
        assert successor.operation_generation == evidence.operation_generation + 1
        assert (successor.root_session_ref, successor.native_session_ref, successor.attempt_ref, successor.fence_ref) == (
            evidence.root_session_ref, evidence.native_session_ref, evidence.attempt_ref, evidence.fence_ref)
        completed = finalizer.finalize(handle=handle, evidence=successor)
        assert completed.status == "completed" and completed.completion_generation == 2
        assert memory.query(completed.manifest_ref) is not None
        assert finalizer.finalize(handle=handle, evidence=successor) == completed
        assert provider.calls == 2
        current = runtime.target_root_lifecycle.query_completion(handle.target_ref)
        assert current.predecessor_completion_ref == rejected_completion.completion_ref
        assert current.predecessor_rejection_ref is not None
    finally:
        runtime.close()
