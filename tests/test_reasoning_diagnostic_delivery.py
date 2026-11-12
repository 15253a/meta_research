"""Real Reasoning adapter/Worker/Owners deliver a refusal back to the same wire session."""

from copy import deepcopy
import json

from meta_research.owners.common import canonical_hash
from meta_research.reasoning_skill import CodexReasoningSkillAdapter
from test_human_guidance_providers import _GuidanceRunner
from test_public_reasoning_autonomous_checkpoint import _checkpoint
from test_reasoning_rejection_diagnostics import _prepare_child_reasoning
from test_reasoning_skill_adapter import _fake_codex
from test_reasoning_summary_decision_flow import final_output, runtime_at


class _CorrectingWireRunner(_GuidanceRunner):
    """Only model output is controlled; transport receipts and Owners are real."""

    def __init__(self):
        super().__init__()
        self.cycle_ref = None
        self.candidates = []

    def __call__(self, argv, prompt, timeout, environment=None):
        request = self.runtime.owners.advancement_engine.query_reasoning_stage_request(self.cycle_ref)
        output = final_output(_checkpoint(request, question_title="A bounded continuation"))
        output["next_cycle_proposal"].update(entry_stage="idea", typed_skip_basis_refs_by_stage={})
        if not self.calls:
            output["scientific_outcome"]["research_synthesis"]["parent_questions"] = []
        self.candidates.append(deepcopy(output))
        self.output = {"provider_output": output}
        return super().__call__(argv, prompt, timeout, environment)


def test_production_wire_receives_durable_parent_feedback_then_delivers_after_restart(tmp_path):
    runner = _CorrectingWireRunner()
    originating_session = runner.native_session_ref
    executable = _fake_codex(tmp_path / "codex")

    def open_runtime():
        adapter = CodexReasoningSkillAdapter(
            tmp_path / "provider", executable=str(executable), process_runner=runner,
        )
        runtime = runtime_at(tmp_path / "runtime", adapter)
        runner.runtime = runtime
        runtime.configure_resident_mcp_endpoint("http://127.0.0.1:8999")
        runtime.reasoning_stage.configure_resident_mcp_endpoint("http://127.0.0.1:8999")
        return runtime

    runtime = open_runtime()
    try:
        runner.cycle_ref, parents = _prepare_child_reasoning(runtime, 1)
        assert len(parents) == 1
        assert parents[0].startswith("question_") and len(parents[0]) == 41
        for _ in range(8):
            assert runtime.reasoning_stage.process_once(), runtime.reasoning_stage.transient_error
            request = runtime.owners.advancement_engine.query_reasoning_stage_request(runner.cycle_ref)
            run = runtime.owners.agent_runtime.query_reasoning_stage_run(request.request_ref)
            if run is not None and run.completion_rejection is not None:
                break
        rejection = run.completion_rejection
        assert rejection is not None
        assert rejection.detail_code == "scientific_outcome_research_synthesis_invalid"
        original_candidate = {"phase": "primary", "result": runner.candidates[0]}
        assert rejection.candidate == original_candidate
        assert rejection.candidate_hash == canonical_hash(original_candidate)
        diagnostic = (
            "scientific_outcome_research_synthesis_invalid:"
            "research_synthesis.parent_questions:missing_impact_for:" + parents[0]
        )
        assert len(diagnostic) > 128
        assert diagnostic in "\n".join(rejection.feedback)
        assert rejection.native_session_ref == run.native_session_ref == originating_session
        assert run.attempt_ref == rejection.successor_attempt_ref
        assert run.fence_ref != rejection.fence_ref
        assert len(runner.calls) == 1

        runtime.close()
        runtime = open_runtime()
        assert runtime.owners.agent_runtime.query_stage_completion_rejection(rejection.attempt_ref) == rejection
        for _ in range(16):
            runtime.reasoning_stage.process_once()
            commit = runtime.owners.advancement_engine.query_reasoning_stage_commit(request.request_ref)
            if commit is not None:
                break
        assert commit is not None, runtime.reasoning_stage.transient_error
        assert len(runner.calls) == 2
        argv, correction_prompt = runner.calls[1]
        assert argv[-3:] == ["resume", originating_session, "-"]
        delivered = json.loads(next(
            line.removeprefix("owner_feedback=") for line in correction_prompt.splitlines()
            if line.startswith("owner_feedback=")
        ))
        assert delivered == list(rejection.feedback)
        assert diagnostic in "\n".join(delivered)
        assert rejection.candidate_ref in correction_prompt
        assert rejection.receipt.receipt_ref in correction_prompt
        assert "accepted_question=" not in correction_prompt
        assert "provider_context_view=" not in correction_prompt
        accepted = runtime.owners.agent_runtime.query_reasoning_stage_run(request.request_ref)
        assert accepted.run_ref == rejection.run_ref
        assert accepted.root_session_ref == rejection.root_session_ref
        assert accepted.native_session_ref == originating_session
        assert accepted.attempt_ref == rejection.successor_attempt_ref
        assert [item["question_ref"] for item in accepted.execution.outcome["scientific_outcome"]["research_synthesis"]["parent_questions"]] == parents
        assert runtime.owners.agent_runtime.query_stage_completion_rejection(rejection.attempt_ref) == rejection
        assert runtime.reasoning_stage.transient_error is None
    finally:
        runtime.close()
