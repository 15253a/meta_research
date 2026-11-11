"""New-data controlled-provider chain through real recovery, Owners and Bundle.

This verifies the engineering chain. The provider's signed terminal seam is
controlled; it is neither native process-tree proof nor real scientific review.
"""

import json
from pathlib import Path
import subprocess
import sys

import pytest

from meta_research.harness import HarnessAdmissionError
from meta_research.owners.common import canonical_json
from test_issue296_stable_workspace import (
    _RecoveryAdapter, _SignedCeilingRunner, _activate, _workspace,
)
from test_target_root_finalizer import (
    _CurrentBindingBundleSkill, _admit_independent_target_root, _current_bundle_runtime,
)


class _CompleteAfterRecovery(_SignedCeilingRunner):
    result_bytes = None

    def _terminal(self, environment):
        if len(self.calls) == 1:
            return super()._terminal(environment)
        assert len(self.calls) == 2
        root = Path(environment["META_RESEARCH_HARNESS_WORKSPACE"])
        # Run retained implementation in the exact recovered cwd. The preceding
        # operation already produced the first half of this same checkpoint.
        script = root / "implementation" / "evaluate.py"
        script.write_text(
            "import json,pathlib\n"
            "state=json.loads(pathlib.Path('issue296-checkpoint.json').read_text())\n"
            "assert state['segments']==2\n"
            "assert state['sum']==sum(i*i for i in range(20000))\n"
            "pathlib.Path('outputs').mkdir(exist_ok=True)\n"
            "pathlib.Path('logs').mkdir(exist_ok=True)\n"
            "pathlib.Path('outputs/final.ckpt').write_text(json.dumps(state,sort_keys=True))\n"
            "pathlib.Path('logs/evaluate.log').write_text('segments=2 sum='+str(state['sum'])+'\\n')\n"
            "print(state['sum'])\n", encoding="utf-8",
        )
        completed = subprocess.run([sys.executable, str(script)], cwd=root,
                                   check=True, capture_output=True, text=True, timeout=10)
        authority = self.runtime.owners.research_graph.query_target_measurement_domain_authority(self.target_ref)
        metrics = {key: float(completed.stdout.strip()) for key in
                   authority.measurement_contract.protocol_version.required_metric_keys}
        document = {
            "schema_ref": authority.measurement_contract.result_schema_ref,
            "result_disposition": "positive", "metrics": metrics,
            "formal_runs": [{"run_key": "issue296-recovered-calculation",
                "artifact_paths": ["logs/evaluate.log"],
                "checkpoint_paths": ["outputs/final.ckpt"],
                "evaluations": [{"attempt_key": "continued", "metrics": metrics, "artifact_paths": []}]}],
            "retained_artifacts": [
                {"role": "checkpoint", "relative_path": "outputs/final.ckpt"},
                {"role": "log", "relative_path": "logs/evaluate.log"}],
        }
        self.result_bytes = canonical_json(document).encode()
        (root / "outputs/result.json").write_bytes(self.result_bytes)
        events = (
            {"type": "thread.started", "thread_id": "issue296-completed-after-recovery"},
            {"type": "item.completed", "item": {"type": "command_execution", "id": "continued-compute",
                "exit_code": 0, "output": completed.stdout}},
            {"type": "item.completed", "item": {"type": "file_change", "id": "continued-files",
                "changes": [{"path": "outputs/result.json", "kind": "add"}]}},
            {"type": "item.completed", "item": {"type": "agent_message",
                "text": "Controlled resumed calculation completed; implementation, checkpoint and result retained."}},
            {"type": "turn.completed"},
        )
        return 0, "completed", "".join(json.dumps(event) + "\n" for event in events)


class _ConsumeRecoveredCommit(_CurrentBindingBundleSkill):
    runtime = None
    expected_commit = None
    expected_result_version = None
    expected_bytes = None
    consumed = None

    def propose_target_batch(self, request):
        assert len(request.target_commits) == 1
        assert request.target_commits[0]["commit_ref"] == self.expected_commit
        material = self.runtime.owners.research_memory.materialize_asset(self.expected_result_version)
        assert material.content == self.expected_bytes
        self.consumed = (self.expected_commit, self.expected_result_version, material.content)
        return super().propose_target_batch(request)


@pytest.mark.skipif(sys.platform != "linux", reason="Linux acceptance")
def test_new_target_recovers_then_formally_hands_off_exact_result_to_bundle(tmp_path):
    runner = _CompleteAfterRecovery()
    bundle = _ConsumeRecoveredCommit()
    root = tmp_path / "issue296-new-continuous-chain"
    runtime = _current_bundle_runtime(root, bundle_skill=bundle,
        harness_adapters=(_RecoveryAdapter(root.with_name(root.name + "-native"), runner),))
    runner.runtime = bundle.runtime = runtime
    runtime.configure_resident_mcp_endpoint("http://127.0.0.1:8999")
    try:
        admitted = _admit_independent_target_root(runtime)
        _activate(runtime, admitted, "issue296-continuous-activate")
        target, _, _, admission, original_handle = admitted
        runner.target_ref = target.target_ref
        original_workspace = _workspace(runtime, original_handle)
        with pytest.raises(HarnessAdmissionError, match="provider_descendant_process"):
            runtime.harnesses.run_or_resume_target_root(admission.run.request_ref,
                prompt="First actual checkpoint segment", mcp_base_url="http://127.0.0.1:8999")
        runtime.harnesses.recover_fenced_target_root(admission.run.request_ref, old_handle=original_handle)
        assert runtime.target_run_runtime.process_once(target.target_ref)
        assert runtime.target_run_runtime.process_once(target.target_ref)
        current = runtime.target_run_authorities.agent_runtime.query_current_target_work_handle(target.target_ref)
        assert current != original_handle
        assert _workspace(runtime, current) == original_workspace
        runtime.harnesses.run_or_resume_target_root(admission.run.request_ref,
            prompt="Resume checkpoint and complete exact result", mcp_base_url="http://127.0.0.1:8999")
        evidence = runtime.harnesses.query_target_root_completion_evidence(target.target_ref)
        assert evidence is not None and evidence.attempt_ref == current.execution_attempt_ref
        assert evidence.workspace_ref == original_workspace[0]
        assert runtime.target_run_runtime.process_once(target.target_ref)
        completion = runtime.target_root_lifecycle.query_completion(target.target_ref)
        assert completion is not None
        result = runtime.target_run_finalizer.finalize(handle=current, evidence=evidence)
        assert result.status == "completed" and result.pending_code is None
        manifest = runtime.target_run_finalizer._memory.query(result.manifest_ref)
        assert manifest is not None and manifest.workspace_ref == original_workspace[0]
        result_entry = next(entry for entry in manifest.entries if entry.role == "result")
        assert runtime.owners.research_memory.materialize_asset(result_entry.binding.version_ref).content == runner.result_bytes
        handoff = runtime.owners.agent_runtime.query_target_root_completion_handoff(
            target_ref=target.target_ref, completion_ref=result.completion_ref,
            target_commit_ref=result.target_commit_ref)
        assert handoff is not None and handoff.handle_history == (original_handle, current)
        assert len(handoff.recovered_blockers) == 1
        notice = runtime.owners.agent_runtime.query_target_work_notice(target.target_ref)
        assert notice is not None and notice.terminal_fact_ref == result.target_commit_ref
        denied, _, _ = runtime.harnesses.dispatch_mcp_http(admission.connection.token,
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}, mcp_session_id=None)
        assert denied == 401
        bundle.expected_commit = result.target_commit_ref
        bundle.expected_result_version = result_entry.binding.version_ref
        bundle.expected_bytes = runner.result_bytes
        for _ in range(12):
            runtime.bundle_stage.process_once()
            if bundle.consumed is not None:
                break
        assert bundle.consumed == (result.target_commit_ref, result_entry.binding.version_ref, runner.result_bytes)
        assert len(runner.calls) == 2
    finally:
        runtime.close()
