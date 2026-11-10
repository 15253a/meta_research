"""The retired agent envelope cannot be re-decoded by current completion reads."""
import json

import pytest
from sqlalchemy import text

import meta_research.owners.agent_runtime_harness as issuer_module
from meta_research.harness import HarnessRuntime
from meta_research.harness_adapters import CodexHarnessAdapter, HarnessSupervisorTransport
from meta_research.owners.common import canonical_hash, canonical_json
from test_harness_stopped_profile_resume import _StoppedThenSuccessfulProvider
from test_target_root_finalizer import _root_finalizer_fixture


@pytest.mark.parametrize("candidate_key", ["target_root_completion_candidate", "target_root_completion_result_candidate"])
def test_repeated_reads_never_decode_retired_agent_envelope(tmp_path, monkeypatch, candidate_key):
    runtime, _lifecycle, _memory, _authority, handle, _workspace, _evidence = _root_finalizer_fixture(tmp_path)
    try:
        owner = runtime.owners.agent_runtime.harness_runs
        store = runtime.harnesses._target_raw_output_store
        provider = _StoppedThenSuccessfulProvider(stop_first=False)
        adapter = CodexHarnessAdapter(tmp_path / "adapter", runner=HarnessSupervisorTransport(
            store._workspace, process_runner=provider,
            event_sink=owner.append_target_root_events, raw_output_store=store))
        harness = HarnessRuntime(owner, runtime.harnesses._gateway, (adapter,), target_raw_output_store=store)
        harness.bind_target_workspace_resolver(runtime.target_run_authorities.agent_runtime)
        request = owner.query_target_run_by_ref(handle.target_run_ref)
        harness.run_or_resume_target_root(request.request_ref, prompt="Finish the Target.", mcp_base_url="http://127.0.0.1:8999")
        evidence = owner.query_target_root_completion_evidence(handle.target_ref)
        candidate = {"artifacts": [{"role": "implementation", "relative_path": "implementation"},
                                   {"role": "result", "relative_path": "outputs/result.json"}],
                     "result_document_path": "outputs/result.json", "summary": "Completed work."}
        if candidate_key == "target_root_completion_candidate":
            candidate.update(schema_ref="meta-research/target-completion-handoff/v1", status="completed",
                             target_ref=handle.target_ref, target_run_ref=handle.target_run_ref)
        with runtime._database.write() as connection:
            rows = connection.execute(text("SELECT event_ref, summary_json FROM ar_harness_evidence_events WHERE operation_ref=:ref"), {"ref": evidence.operation_ref}).all()
            row = next(row for row in rows if json.loads(row.summary_json).get("target_root_agent_message"))
            event = json.loads(row.summary_json)
            event.pop("target_root_completion_binding")
            event[candidate_key] = candidate
            connection.execute(text("UPDATE ar_harness_evidence_events SET summary_json=:value, summary_hash=:hash WHERE event_ref=:ref"),
                               {"value": canonical_json(event), "hash": canonical_hash(event), "ref": row.event_ref})
        counts = {"reads": 0, "legacy_decode": 0, "accepted": 0, "rejected": 0}
        decoder = getattr(issuer_module, "decode_target_completion_handoff", None)
        if decoder is not None:
            def counted_decoder(*args, **kwargs):
                counts["legacy_decode"] += 1
                return decoder(*args, **kwargs)
            monkeypatch.setattr(issuer_module, "decode_target_completion_handoff", counted_decoder)
        for _ in range(4):
            counts["reads"] += 1
            try:
                observed = owner.query_target_root_completion_evidence(handle.target_ref)
                counts["accepted"] += observed is not None
            except issuer_module.AgentRuntimeHarnessError as error:
                assert error.code == "target_root_completion_evidence_invalid"
                counts["rejected"] += 1
        print(json.dumps({"candidate_key": candidate_key, **counts}, sort_keys=True))
        assert provider.calls == 1
        assert counts == {"reads": 4, "legacy_decode": 0, "accepted": 0, "rejected": 4}
    finally:
        runtime.close()
