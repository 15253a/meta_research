from dataclasses import replace
import json

import pytest

from bundle_resume_samples import capture_bundle_resume_inputs
from meta_research.bundle_skill import BundleSkillRequest
from meta_research.idea_skill import IdeaSkillUnavailable
from meta_research.owners.common import canonical_hash
from test_bundle_input_drift_recovery import _adapter, _output, _request
from test_bundle_native_replacement import _replacement_context
from test_public_guidance_delivery import _developer
from test_bundle_skill_adapter import _inbox_checkpoint, _plan_document, _question_content, _target_plan
from test_gemini_batch_delta import _source_readers


def _view(prompt, name):
    return json.loads(next(line[len(name) + 1:] for line in prompt.splitlines() if line.startswith(name + "=")))


def test_current_bundle_provider_inputs_keep_new_facts_without_old_bodies(tmp_path):
    adapter, runner, requests, samples = capture_bundle_resume_inputs(tmp_path)
    first_dispatch, first_batch, continued, later, correction = samples
    assert "ORIGINAL_CANDIDATE_DETAIL" in first_dispatch["prompt"]
    assert first_dispatch["prompt"].count("ORIGINAL_CANDIDATE_DETAIL") == 100
    assert "ORIGINAL_CANDIDATE_DETAIL" not in first_batch["prompt"]
    assert "FIRST_EXACT_RESULT" in first_batch["prompt"]
    assert "ORIGINAL_CANDIDATE_DETAIL" not in continued["prompt"]
    frontier = _view(continued["prompt"], "frontier")
    assert frontier[0]["dispatch_allowed"] is False
    assert frontier[0]["human_request_command"] == requests[2].frontier[0]["human_request_command"]
    assert _view(continued["prompt"], "state") == requests[2].state
    assert "FIRST_EXACT_RESULT" not in later["prompt"]
    assert "NEW_COUNTEREXAMPLE" in later["prompt"]
    assert _view(later["prompt"], "batch_context")["target_commits"] == [requests[3].target_commits[-1]]
    assert "THIS_CORRECTION_ONLY" in correction["prompt"]
    assert "FIRST_EXACT_RESULT" not in correction["prompt"]
    assert "NEW_COUNTEREXAMPLE" not in correction["prompt"]
    assert "ORIGINAL_CANDIDATE_DETAIL" not in correction["prompt"]
    assert "formal_plan" not in _view(correction["prompt"], "batch_context")
    assert all(sample["argv"][-3:] == ["resume", requests[0].native_session_ref, "-"] for sample in samples)
    assert len({_developer(sample["argv"]) for sample in samples}) == 1
    operation = adapter._workspace / "provider-operations" / canonical_hash({"job_ref": requests[-1].job_ref}) / "target-batch-2"
    sealed = {path.name: path.read_bytes() for path in operation.iterdir() if path.is_file()}
    frozen = json.loads(sealed["business-input.json"])
    assert frozen["request"]["target_commits"] == list(requests[-1].target_commits)
    adapter.propose_target_batch(requests[-1])
    assert len(runner.calls) == len(samples)
    assert {name: (operation / name).read_bytes() for name in sealed} == sealed


def test_dispatch_new_candidate_and_reverted_value_arrive_in_full(tmp_path):
    adapter, runner = _adapter(tmp_path, _output("dispatch"), durable=True)
    request = _request(adapter, "dispatch")
    first = {**request.frontier[0], "spec": {"candidate": {"research_text": "FIRST_VALUE"}}}
    request = replace(request, frontier=(first,))
    runner._outputs = iter([_output("dispatch")] * 4)
    adapter.schedule_targets(request)
    fresh = {**first, "target_ref": "target:fresh", "spec": {"candidate": {"research_text": "NEW_CANDIDATE_BODY"}}}
    adapter.schedule_targets(replace(request, generation=2, frontier=(first, fresh)))
    assert "FIRST_VALUE" not in runner.calls[1][1]
    assert "NEW_CANDIDATE_BODY" in runner.calls[1][1]
    revised = {**first, "spec": {"candidate": {"research_text": "SECOND_VALUE"}}}
    adapter.schedule_targets(replace(request, generation=3, frontier=(revised,)))
    assert "SECOND_VALUE" in runner.calls[2][1]
    adapter.schedule_targets(replace(request, generation=4))
    assert "FIRST_VALUE" in runner.calls[3][1]


def test_unchanged_dispatch_body_stays_absent_after_multiple_resume_turns(tmp_path):
    adapter, runner = _adapter(tmp_path, _output("dispatch"), durable=True)
    request = _request(adapter, "dispatch")
    target = {**request.frontier[0], "dispatch_allowed": True,
              "spec": {"candidate": {"research_text": "INITIAL_CANDIDATE_ONLY"}}}
    request = replace(request, frontier=(target,))
    runner._outputs = iter([_output("dispatch")] * 4)
    for generation in range(1, 5):
        adapter.schedule_targets(replace(request, generation=generation))
    assert "INITIAL_CANDIDATE_ONLY" in runner.calls[0][1]
    for _, prompt, _ in runner.calls[1:]:
        assert "INITIAL_CANDIDATE_ONLY" not in prompt
        assert "spec" not in _view(prompt, "frontier")[0]
        assert _view(prompt, "frontier")[0]["dispatch_allowed"] is True


@pytest.mark.parametrize("kind", ["dispatch", "batch"])
def test_replacement_does_not_inherit_prior_native_delivery(tmp_path, kind):
    adapter, runner = _adapter(tmp_path, _output(kind), durable=True)
    request = _request(adapter, kind)
    invoke = adapter.schedule_targets if kind == "dispatch" else adapter.propose_target_batch
    invoke(request)
    replacement = replace(request, job_ref="replacement-job", native_session_ref=None,
                          native_replacement_context=_replacement_context(request))
    invoke(replacement)
    assert "resume" not in runner.calls[-1][0]
    assert "Controlled replacement" in runner.calls[-1][1]
    view = _view(runner.calls[-1][1], "frontier" if kind == "dispatch" else "batch_context")
    assert view == list(request.frontier) if kind == "dispatch" else view["target_commits"] == list(request.target_commits)


@pytest.mark.parametrize("damage", ["prompt", "pending", "foreign_root"])
def test_unproven_history_does_not_suppress_new_provider_input(tmp_path, damage):
    adapter, runner = _adapter(tmp_path, _output("dispatch"), durable=True)
    request = _request(adapter, "dispatch")
    target = {**request.frontier[0], "spec": {"candidate": {"research_text": "MUST_ARRIVE"}}}
    request = replace(request, frontier=(target,))
    adapter.schedule_targets(request)
    operation = adapter._workspace / "provider-operations" / canonical_hash({"job_ref": request.job_ref}) / "dispatch-1"
    if damage == "prompt":
        (operation / "prompt.txt").write_text("tampered", encoding="utf-8")
    elif damage == "pending":
        (operation / "completed.json").unlink()
    else:
        request = replace(request, root_session_ref="different-logical-root")
    adapter.schedule_targets(replace(request, generation=2))
    assert "MUST_ARRIVE" in runner.calls[-1][1]


def test_unknown_same_operation_never_becomes_a_new_delta_turn(tmp_path):
    adapter, runner, requests, _ = capture_bundle_resume_inputs(tmp_path)
    request = requests[-1]
    operation = adapter._workspace / "provider-operations" / canonical_hash({"job_ref": request.job_ref}) / "target-batch-2"
    for name in ("completed.json", "exit.json", "supervisor-exit.json"):
        (operation / name).unlink(missing_ok=True)
    original = (operation / "prompt.txt").read_bytes()
    with pytest.raises(IdeaSkillUnavailable, match="codex_operation_reconciliation_pending"):
        adapter.propose_target_batch(replace(request, target_commits=(*request.target_commits,
            {"commit_ref": "unknown-arrival", "target_ref": "target:new"})))
    assert len(runner.calls) == 5
    assert (operation / "prompt.txt").read_bytes() == original


def test_correction_with_new_result_delivers_both_without_old_bodies(tmp_path):
    adapter, runner, requests, _ = capture_bundle_resume_inputs(tmp_path)
    output = _output("batch")
    output["strategy_update"]["revision"] = 3
    runner._outputs = iter([output])
    new_result = {"commit_ref": "commit:correction-arrival", "target_ref": "target:ready",
                  "scientific_result": "RESULT_ARRIVING_WITH_CORRECTION", "precise": "1.0000000000002"}
    request = replace(requests[-1], job_ref="correction-with-arrival",
                      target_commits=(*requests[-1].target_commits, new_result))
    adapter.propose_target_batch(request)
    prompt = runner.calls[-1][1]
    assert "THIS_CORRECTION_ONLY" in prompt
    assert _view(prompt, "batch_context")["target_commits"] == [new_result]
    assert "FIRST_EXACT_RESULT" not in prompt
    assert "NEW_COUNTEREXAMPLE" not in prompt


def test_unavailable_exact_source_keeps_needed_result_body(tmp_path):
    adapter, runner, requests, _ = capture_bundle_resume_inputs(tmp_path)
    output = _output("batch")
    output["strategy_update"]["revision"] = 3
    runner._outputs = iter([output])
    adapter.bind_batch_source_readers(lambda request: {**_source_readers(request),
        "target_commits": {}, "proposals": ()})
    request = replace(requests[-1], job_ref="missing-exact-result-reader")
    adapter.propose_target_batch(request)
    assert _view(runner.calls[-1][1], "batch_context")["target_commits"] == list(request.target_commits)


def test_primary_initializes_once_then_delivers_only_current_correction(tmp_path):
    plan = _plan_document()
    question = _question_content()
    context = {"schema_ref": "meta-research/bundle-context-pack/v1", "cycle_ref": "cycle:bundle-1",
        "cycle_position": {"cycle_ref": "cycle:bundle-1", "round": 1,
                           "cycle_focus": "INITIAL_ONLY_RESEARCH_FOCUS", "focus_source": {"request_ref": "fixture-start"}},
        "accepted_question_binding": {"question_ref": "question:bundle-1", "content_hash": canonical_hash(question)},
        "accepted_formal_plan_binding": {"formal_plan_ref": "formal-plan:bundle-1", "plan_document": plan,
                                       "plan_document_hash": canonical_hash(plan), "answer_contract_hash": "a" * 64}}
    output = {"target_plan": _target_plan(plan, canonical_hash(context))}
    adapter, runner = _adapter(tmp_path, output, durable=True)
    initial = BundleSkillRequest(accepted_question_content=question, stage_request_ref="stage-request:bundle-1",
        run_ref="bundle-run:1", attempt_ref="bundle-attempt:1", fence_ref="bundle-fence:1",
        cycle_ref="cycle:bundle-1", question_ref="question:bundle-1", formal_plan_ref="formal-plan:bundle-1",
        context_pack_ref="context-pack:bundle-1", context_pack_hash=canonical_hash(context), context_pack=context,
        plan_document=plan, root_session_ref="ar-session:bundle-1", runtime_binding=adapter.runtime_binding(),
        inbox_checkpoint=_inbox_checkpoint(run_ref="bundle-run:1", attempt_ref="bundle-attempt:1", fence_ref="bundle-fence:1"),
        job_ref="primary-initial")
    draft = adapter.generate_draft(initial)
    continued = replace(initial, native_session_ref=draft.primary_session_ref, job_ref="primary-correction",
        predecessor_candidate_ref="candidate:rejected", owner_rejection_receipt_ref="receipt:rejected",
        owner_rejection_kind="completion", owner_feedback=("THIS_PRIMARY_CORRECTION",))
    adapter.generate_draft(continued)
    first_argv, first_prompt, first_schema = runner.calls[0]
    later_argv, later_prompt, later_schema = runner.calls[1]
    assert "resume" not in first_argv
    assert "accepted_question=" in first_prompt and "provider_context_view=" in first_prompt
    assert "INITIAL_ONLY_RESEARCH_FOCUS" in first_prompt
    assert later_argv[-3:] == ["resume", draft.primary_session_ref, "-"]
    assert "THIS_PRIMARY_CORRECTION" in later_prompt and "candidate:rejected" in later_prompt
    assert "accepted_question=" not in later_prompt and "provider_context_view=" not in later_prompt
    assert "INITIAL_ONLY_RESEARCH_FOCUS" not in later_prompt
    assert _developer(first_argv) == _developer(later_argv)
    assert first_schema == later_schema
