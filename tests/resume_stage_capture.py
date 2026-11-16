"""Repeatable input samples at the Codex process-runner boundary."""
from __future__ import annotations

from dataclasses import fields, replace
from copy import deepcopy
import json
from pathlib import Path

from meta_research.idea_skill import CodexIdeaSkillAdapter
from meta_research.plan_skill import CodexPlanSkillAdapter
from meta_research.reasoning_skill import CodexReasoningSkillAdapter
from meta_research.owners.common import canonical_hash
import test_idea_skill_contract as idea
import test_plan_skill_adapter as plan
import test_reasoning_skill_adapter as reasoning
from provider_supervisor_support import closed_tree_fixture


def make_adapter(stage, path, outputs):
    path.mkdir(parents=True, exist_ok=True)
    module = {"idea": idea, "plan": plan, "reasoning": reasoning}[stage]

    class BoundaryRunner(module._SequenceRunner):
        def __call__(self, argv, prompt, timeout, environment=None):
            completed = super().__call__(argv, prompt, timeout, environment)
            completed.process_tree = closed_tree_fixture()
            return completed

        def run_job(self, job_ref, argv, prompt, timeout, environment=None):
            return self(argv, prompt, timeout, environment)

    runner = BoundaryRunner(outputs)
    adapter_type = {"idea": CodexIdeaSkillAdapter, "plan": CodexPlanSkillAdapter,
                    "reasoning": CodexReasoningSkillAdapter}[stage]
    executable = plan._fake_codex(path / "codex")
    adapter = adapter_type(path / "provider", executable=str(executable), process_runner=runner)
    if stage == "reasoning":
        adapter.bind_full_conformance_authority(reasoning._FullConformanceAuthority())
        adapter.configure_resident_mcp_endpoint("http://semantic-mcp.invalid")
    request = replace(module._request(), runtime_binding=adapter.runtime_binding())
    return adapter, request, runner


def continuation_request(request, job_ref, predecessor=None, feedback=()):
    changes = {"job_ref": job_ref, "continuation_feedback": feedback,
               "current_continuation_feedback": feedback,
               "continuation_predecessor_job_ref": predecessor}
    known = {field.name for field in fields(request)}
    return replace(request, **{name: value for name, value in changes.items() if name in known})


def capture_primary(stage, path):
    output = {"idea": lambda: {"outcome": idea._idea_set()},
              "plan": lambda: {"plan": plan._plan()},
              "reasoning": reasoning._stage_output}[stage]()
    adapter, request, runner = make_adapter(stage, path, [output] * 4)
    request = replace(request, job_ref="sample-initial")
    initial = adapter.generate_draft(request)
    continued = replace(request, native_session_ref=initial.primary_session_ref, job_ref="sample-continue")
    adapter.generate_draft(continued)
    correction = {"job_ref": "sample-correct", "owner_rejection_kind": "completion",
                  "owner_rejection_receipt_ref": "receipt:current-feedback",
                  "owner_feedback": ("CURRENT_FEEDBACK 请补充实际证据。",)}
    correction["predecessor_candidate_ref" if stage == "reasoning" else "predecessor_submission_ref"] = "submission:rejected"
    if stage != "reasoning":
        correction["submission_revision"] = 2
    corrected = replace(continued, **correction)
    adapter.generate_draft(corrected)
    sealed = (adapter._workspace / "provider-operations"
              / canonical_hash({"job_ref": corrected.job_ref}) / "primary" / "prompt.txt")
    sealed_bytes = sealed.read_bytes()
    adapter.generate_draft(corrected)
    assert len(runner.calls) == 3
    assert sealed.read_bytes() == sealed_bytes
    replacement = replace(request, job_ref="sample-replacement", native_session_ref=None,
                          native_replacement_context={"unresolved": "REPLACEMENT_INPUT"})
    adapter.generate_draft(replacement)
    return {name: {"prompt": call[1], "argv": call[0], "schema": call[2]}
            for name, call in zip(("initial", "continue", "correction", "replacement"), runner.calls)}


def capture_autonomous(path):
    base = reasoning._request()
    checkpoint, result, review = reasoning._autonomous_resume_fixture(base)
    decisions = [{"action": "create", "final_output": checkpoint}] * 4
    adapter, request, runner = make_adapter("reasoning", path, decisions + [review] * 3)
    request = replace(request, native_session_ref="provider-session:1")
    facts = {"status": "succeeded", "request_ref": "deepfetch:first", "snapshot_ref": "snapshot:first", "detail": "NEW_FACTS"}
    summary = {"source_ref": "snapshot:first", "summary": "NEW_SUMMARY " * 80}
    summary_request = continuation_request(request, "summary-first")
    adapter.decide_after_deepfetch(summary_request, checkpoint, facts, summary)
    correction = continuation_request(request, "summary-correct", "summary-first", ("SUMMARY_FEEDBACK",))
    adapter.decide_after_deepfetch(correction, checkpoint, facts, summary)
    changed = continuation_request(request, "summary-changed", "summary-correct", ("CHANGED_FEEDBACK",))
    changed = replace(changed, continuation_feedback=("SUMMARY_FEEDBACK", "CHANGED_FEEDBACK"))
    changed_facts = {**facts, "detail": "CHANGED_FACTS"}
    changed_summary = {**summary, "summary": "CHANGED_SUMMARY " * 90}
    adapter.decide_after_deepfetch(changed, checkpoint, changed_facts, changed_summary)
    changed_checkpoint = deepcopy(checkpoint)
    changed_checkpoint["scientific_outcome"]["notes"] = "CHANGED_CHECKPOINT"
    checkpoint_request = continuation_request(request, "checkpoint-changed", "summary-changed", ("CHECKPOINT_FEEDBACK",))
    adapter.decide_after_deepfetch(checkpoint_request, changed_checkpoint, changed_facts, changed_summary)
    final_request = continuation_request(request, "creation-first")
    adapter.resume_after_autonomous_creation(final_request, checkpoint, result)
    final_correction = continuation_request(request, "creation-correct", "creation-first", ("CREATION_FEEDBACK",))
    adapter.resume_after_autonomous_creation(final_correction, checkpoint, result)
    final_changed = continuation_request(request, "creation-changed", "creation-correct", ("RESULT_FEEDBACK",))
    changed_result = {**result, "new_material": "NEW_CREATION_RESULT"}
    adapter.resume_after_autonomous_creation(final_changed, checkpoint, changed_result)
    return {name: {"prompt": call[1], "argv": call[0], "schema": call[2]}
            for name, call in zip(("summary-first", "summary-correction", "facts-changed", "checkpoint-changed",
                                   "creation-first", "creation-correction", "creation-changed"), runner.calls)}


def capture_all(path):
    return {**{stage: capture_primary(stage, path / stage)
               for stage in ("idea", "plan", "reasoning")},
            "reasoning-autonomous": capture_autonomous(path / "autonomous")}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("workspace", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    samples = capture_all(args.workspace)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(samples, ensure_ascii=False, indent=2), encoding="utf-8")
