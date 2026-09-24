from copy import deepcopy
from dataclasses import replace
import json

import pytest

from meta_research.owners.common import canonical_hash, canonical_json
from meta_research.idea_contract import IdeaContractError, material_outcome_hash, validate_idea_outcome
from meta_research.idea_skill import CodexIdeaSkillAdapter
from meta_research.plan_skill import CodexPlanSkillAdapter, validate_plan_skill_result
from meta_research.reasoning_skill import CodexReasoningSkillAdapter
from meta_research.bundle_target_contract import (
    FORMAL_STRATEGY_UPDATE_SCHEMA_REF, normalized_completion_contract_from_dict,
    strategy_update_from_dict, strategy_update_to_dict,
)
import test_idea_skill_contract as idea_fixtures
import test_plan_skill_adapter as plan_fixtures
import test_reasoning_skill_adapter as reasoning_fixtures
import test_public_reasoning_owners as reasoning_owners
import test_public_bundle_stage as bundle_fixtures
import test_bundle_skill_adapter as bundle_adapter_fixtures
import test_bundle_report_receipt_order as report_fixtures

NOTE = "现场备注：访谈样本存在缺口；下一轮先核实档案。\n保留假设 α，不把尚未完成的观察当作结论。"
LATE_NOTE = "实施后备注：新发现缺失访谈记录；保留不一致案例，后续补充证据。"


def _context_preview(prompt, context_hash):
    lines = [line for line in prompt.splitlines() if line.startswith("provider_context_view=")]
    assert len(lines) == 1
    preview = json.loads(lines[0].split("=", 1)[1])
    assert preview["summary_only"] is True
    assert preview["source_context_pack_hash"] == context_hash
    assert preview["reader"]["operation"] == "research_memory.stage_context.read"
    return preview["sections"]


def _preview_rows(value):
    return value if isinstance(value, list) else value["items"]


def test_compact_idea_is_accepted_and_notes_are_not_rewritten():
    old = idea_fixtures._idea_set()
    old_hash = canonical_hash(old)
    assert validate_idea_outcome(old, question_ref="question:1", context_pack_ref="context-pack:1", accepted_evidence_refs={"asset:accepted-1"}) == old_hash
    compact = deepcopy(old)
    compact["notes"] = NOTE
    for candidate in compact["candidates"]:
        for key in ("assumptions", "risks", "falsification_hint", "material_difference"):
            candidate.pop(key)
        candidate["notes"] = NOTE
    assert validate_idea_outcome(compact, question_ref="question:1", context_pack_ref="context-pack:1", accepted_evidence_refs={"asset:accepted-1"}) == canonical_hash(compact)
    assert compact["notes"] == NOTE
    assert canonical_hash(old) == old_hash and "notes" not in old
    compact["notes"] = {"claim": "not a free text note"}
    with pytest.raises(IdeaContractError, match="idea_notes_invalid"):
        validate_idea_outcome(compact, question_ref=None, context_pack_ref=None, accepted_evidence_refs=None)


def test_idea_method_notes_distinguish_material_candidates_and_successors():
    outcome = idea_fixtures._idea_set()
    first = outcome["candidates"][0]
    for key in ("assumptions", "risks", "falsification_hint", "material_difference"):
        first.pop(key)
    first["notes"] = "方法：用参与式观察检验口述材料与现场行为是否一致。"
    second = {**deepcopy(first), "candidate_key": "archival-comparison", "notes": "方法：用历史档案比较检验回忆偏差；核对同期独立记录。"}
    outcome["candidates"].append(second)
    validate_idea_outcome(outcome, question_ref=None, context_pack_ref=None, accepted_evidence_refs=None)
    before = material_outcome_hash(outcome)
    successor = deepcopy(outcome)
    successor["candidates"][0]["notes"] = "方法修订：加入盲法独立观察员，检验观察者效应。"
    assert material_outcome_hash(successor) != before


def test_blank_idea_notes_equal_absence_without_changing_legacy_material_hash():
    legacy = idea_fixtures._idea_set()
    before = material_outcome_hash(legacy)
    empty = deepcopy(legacy)
    empty["notes"] = " \n\t"
    for candidate in empty["candidates"]:
        candidate["notes"] = ""
    assert material_outcome_hash(empty) == before
    assert "notes" not in legacy


def test_actual_plan_provider_accepts_compact_output_and_passes_idea_notes(tmp_path):
    plan = deepcopy(plan_fixtures._plan())
    plan["notes"] = NOTE
    omitted = ("evidence_reuse_set", "gap_set", "idea_trace", "bundle_disposition", "source_bindings")
    for field in omitted:
        plan.pop(field)
    plan["answer_contract"].pop("answer_contract_hash", None)
    runner = plan_fixtures._SequenceRunner([
        {"plan": plan}, { "final_plan": plan, },
    ])
    adapter = CodexPlanSkillAdapter(tmp_path / "provider", executable=str(plan_fixtures._fake_codex(tmp_path / "codex")), process_runner=runner)
    idea = deepcopy(plan_fixtures._IDEA_SET)
    idea["notes"] = NOTE
    context = deepcopy(plan_fixtures._context_pack())
    context["accepted_idea_set_binding"]["idea_set"] = idea
    context["accepted_idea_set_binding"]["outcome_hash"] = canonical_hash(idea)
    request = plan_fixtures._request(runtime_binding=adapter.runtime_binding(), accepted_idea_set=idea, context_pack=context, context_pack_hash=canonical_hash(context))
    result = adapter.execute(request)
    validate_plan_skill_result(request, result)
    assert result.final_plan["notes"] == NOTE
    assert result.final_plan["evidence_reuse_set"] == []
    assert result.final_plan["gap_set"] == [row["obligation_key"] for row in plan["coverage"]]
    for _, prompt, schema in runner.calls:
        assert canonical_json(idea) in prompt
        output_key = "plan" if "plan" in schema["properties"] else "final_plan"
        properties = schema["properties"][output_key]["properties"]
        assert "notes" in properties
        assert not set(omitted) & set(properties)


def _reasoning_prompt(tmp_path, notes):
    tmp_path.mkdir(parents=True, exist_ok=True)
    output = reasoning_fixtures._stage_output()
    output["scientific_outcome"]["notes"] = NOTE
    review = {"schema_ref": reasoning_fixtures.REASONING_REVIEW_SCHEMA_REF,  "final_output": output, }
    runner = reasoning_fixtures._SequenceRunner([output, review])
    adapter = CodexReasoningSkillAdapter(tmp_path / "provider", executable=str(reasoning_fixtures._fake_codex(tmp_path / "codex")), process_runner=runner)
    adapter.bind_full_conformance_authority(reasoning_fixtures._FullConformanceAuthority())
    adapter.configure_resident_mcp_endpoint("http://127.0.0.1:8765")
    request = reasoning_fixtures._request()
    context = deepcopy(request.context_pack)
    context["upstream_stage_closure"][-1]["closure"] = notes
    request = replace(request, runtime_binding=adapter.runtime_binding(), context_pack=context, context_pack_hash=canonical_hash(context))
    draft = adapter.generate_draft(request)
    result = adapter.review_draft(replace(request, native_session_ref=draft.primary_session_ref), draft)
    assert result.scientific_outcome["notes"] == NOTE
    preview = _context_preview(runner.calls[0][1], request.context_pack_hash)
    assert _preview_rows(preview["upstream_stage_closure"])[-1]["closure"] == notes
    return runner.calls[0][1]


def test_actual_reasoning_provider_consumes_bundle_notes(tmp_path):
    _reasoning_prompt(tmp_path, {"notes": LATE_NOTE, "notes_source_ref": "proposal:accepted"})


def test_accepted_reasoning_notes_reach_next_idea_provider(tmp_path, monkeypatch):
    database, memory, _, graph, receipts = reasoning_owners._owners(tmp_path)
    original = memory.accept_reasoning_content
    def accept_with_notes(**values):
        values["outcome"]["scientific_outcome"]["notes"] = NOTE
        values["reviewed_draft"] = deepcopy(values["outcome"])
        values["review"] = reasoning_owners._review(values["outcome"])
        return original(**values)
    monkeypatch.setattr(memory, "accept_reasoning_content", accept_with_notes)
    try:
        content = reasoning_owners._accept_real_root_next_cycle_content(memory, graph, submission_ref="reasoning-submission:notes", outcome_ref="scientific-outcome:notes")
        accepted = graph.decide_reasoning_outcome(content=content)
        assert accepted.decision == "accepted"
        closure = receipts.query_reasoning_transition_binding(accepted.outcome_ref, accepted.receipt)
        assert closure["notes"] == NOTE
        assert closure["scientific_outcome_hash"] == content.outcome_hash
        outcome = idea_fixtures._idea_set()
        runner = idea_fixtures._SequenceRunner([{"outcome": outcome}, idea_fixtures._review_turn_output()])
        adapter = CodexIdeaSkillAdapter(tmp_path / "idea-provider", process_runner=runner)
        context = deepcopy(idea_fixtures._request().context_pack)
        context["prior_accepted_bindings"] = [{"closure": closure, "outcome_ref": accepted.outcome_ref}]
        request = idea_fixtures._request(runtime_binding=adapter.runtime_binding(), context_pack=context, context_pack_hash=canonical_hash(context))
        adapter.execute(request)
        preview = _context_preview(runner.calls[0][1], request.context_pack_hash)
        prior = _preview_rows(preview["prior_accepted_bindings"])[0]
        assert prior["outcome_ref"] == accepted.outcome_ref
        assert prior["closure"]["notes"] == NOTE
    finally:
        database.close()


@pytest.mark.parametrize("notes", [None, "", NOTE])
def test_strategy_notes_preserve_old_serialization_and_exact_text(notes):
    plan = bundle_adapter_fixtures._plan_document()
    target_plan = bundle_adapter_fixtures._target_plan(plan, "a" * 64)
    completion = normalized_completion_contract_from_dict(target_plan["completion_contract"], plan_document=plan)
    value = target_plan["initial_strategy_update"]
    if notes is not None:
        value["notes"] = notes
    before = canonical_hash(value)
    parsed = strategy_update_from_dict(value, completion_contract=completion)
    encoded = strategy_update_to_dict(parsed, completion_contract=completion)
    assert canonical_hash(encoded) == before
    assert encoded == value


@pytest.mark.parametrize("seal_notes", [None, ""])
def test_late_strategy_notes_are_accepted_and_reach_reasoning_prompt(tmp_path, seal_notes):
    class NotesBundle(bundle_fixtures._DeterministicBundleSkill):
        def _target_plan(self, request):
            value = super()._target_plan(request)
            value["notes"] = NOTE
            return value
    runtime = bundle_fixtures._bundle_runtime(tmp_path / "late-notes", bundle_skill_provider=NotesBundle(), plan_skill_provider=bundle_fixtures._TwoGapPlanSkill())
    try:
        bundle_fixtures._prepare_bundle_request(runtime)
        request_ref = runtime.bundle_stage.query_current()["stage_run_request"]["request_ref"]
        for _ in range(10):
            runtime.bundle_stage.process_once()
            graph = runtime.owners.research_graph.query_target_graph(request_ref)
            if graph is not None:
                break
        assert graph is not None
        run = runtime.owners.agent_runtime.query_bundle_stage_run(request_ref)
        inbox = runtime.owners.agent_runtime.query_bundle_inbox_checkpoint(run.run_ref)
        second_key = "compare-artifact-boundary"
        second_spec = bundle_fixtures._formal_candidate(
            completion_document=graph.target_plan["completion_contract"],
            label="late-notes-followup", experiment_key=second_key,
            cell=f"measurement:{second_key}", depends_on=(),
        )
        proposal = runtime.owners.agent_runtime.record_bundle_target_proposal(
            run_ref=run.run_ref, attempt_ref=run.attempt_ref, fence_ref=run.fence_ref,
            native_session_ref=run.native_session_ref, graph_ref=graph.graph_ref,
            base_generation=graph.head_generation, base_head_receipt=graph.head_receipt,
            strategy_update={"schema_ref": FORMAL_STRATEGY_UPDATE_SCHEMA_REF, "revision": 2, "candidates": [second_spec], "requires_accepted_labels": [], "strategy_complete": False, "notes": LATE_NOTE},
            inbox_checkpoint=inbox, idempotency_key="late-free-notes",
        )
        runtime.owners.research_graph.append_target_batch(graph_ref=graph.graph_ref, proposal_ref=proposal.proposal_ref, proposal=proposal.proposal, proposal_hash=proposal.proposal_hash, proposal_receipt=proposal.receipt)
        graph = runtime.owners.research_graph.query_target_graph(request_ref)
        seal_update = {"schema_ref": FORMAL_STRATEGY_UPDATE_SCHEMA_REF, "revision": 3, "candidates": [], "requires_accepted_labels": [], "strategy_complete": True}
        if seal_notes is not None:
            seal_update["notes"] = seal_notes
        seal = runtime.owners.agent_runtime.record_bundle_target_proposal(
            run_ref=run.run_ref, attempt_ref=run.attempt_ref, fence_ref=run.fence_ref,
            native_session_ref=run.native_session_ref, graph_ref=graph.graph_ref,
            base_generation=graph.head_generation, base_head_receipt=graph.head_receipt,
            strategy_update=seal_update, inbox_checkpoint=runtime.owners.agent_runtime.query_bundle_inbox_checkpoint(run.run_ref),
            idempotency_key="seal-keeps-late-notes",
        )
        runtime.owners.research_graph.append_target_batch(graph_ref=graph.graph_ref, proposal_ref=seal.proposal_ref, proposal=seal.proposal, proposal_hash=seal.proposal_hash, proposal_receipt=seal.receipt)
        graph = runtime.owners.research_graph.query_target_graph(request_ref)
        for _ in range(4):
            runtime.bundle_stage.process_once()
            projection = runtime.owners.research_graph.query_target_formal_plan_projection(graph_ref=graph.graph_ref)
            if projection is not None:
                break
        assert projection is not None
        contract = runtime.owners.research_graph.query_bundle_report_contract(
            request_ref=request_ref, run_ref=run.run_ref, graph_ref=graph.graph_ref,
            head_receipt=graph.head_receipt, formal_plan_content_receipt=projection.source_acceptance_receipt,
            formal_plan_projection_receipt=projection.receipt,
        )
        assert contract["notes"] == LATE_NOTE
        assert contract["notes_source_ref"] == proposal.proposal_ref
        assert contract["notes_source_hash"] == canonical_hash(proposal.proposal)
        _reasoning_prompt(tmp_path / "downstream", {key: contract[key] for key in ("notes", "notes_source_ref", "notes_source_hash")})
    finally:
        runtime.close()


def test_late_notes_survive_real_stage_commit_and_reasoning_request(tmp_path, monkeypatch):
    class LateNotesTargets(report_fixtures._TwoCurrentTargets):
        def propose_target_batch(self, request):
            result = super().propose_target_batch(request)
            return replace(result, strategy_update={**result.strategy_update, "notes": LATE_NOTE})
    monkeypatch.setattr(report_fixtures, "_TwoCurrentTargets", LateNotesTargets)
    fixture = report_fixtures.committed_targets.__wrapped__(tmp_path)
    runtime, graph, run, _, source, projection = next(fixture)
    try:
        for _ in range(8):
            if graph.strategy_complete:
                break
            runtime.bundle_stage.process_once()
            graph = runtime.owners.research_graph.query_target_graph(run.request_ref)
        assert graph.strategy_complete
        owner = runtime.owners.agent_runtime
        values = dict(run_ref=run.run_ref, attempt_ref=run.attempt_ref, fence_ref=run.fence_ref,
            formal_plan_content_receipt=source.receipt, formal_plan_projection_receipt=projection.receipt,
            target_graph_ref=graph.graph_ref, target_graph_receipt=graph.head_receipt)
        candidate = owner.build_bundle_report_candidate(disposition="realized", **values)
        accepted = owner.accept_bundle_report(report=candidate, idempotency_key="notes-report", **values)
        completion = owner.complete_bundle_run(run_ref=run.run_ref, attempt_ref=run.attempt_ref,
            fence_ref=run.fence_ref, report_ref=accepted.report_ref, decision_receipt=accepted.receipt,
            idempotency_key="notes-complete")
        ae = runtime.owners.advancement_engine
        commit = ae.commit_bundle_stage(request_ref=run.request_ref, run_ref=run.run_ref,
            bundle_report_ref=accepted.report_ref, run_completion_receipt=completion.receipt,
            bundle_report_receipt=accepted.receipt, idempotency_key="notes-advance")
        assert commit.closure["notes"] == LATE_NOTE
        assert ae.query_bundle_stage_commit(run.request_ref).closure == commit.closure
        bundle_request = ae.query_bundle_stage_request(graph.cycle_ref)
        requested = ae.ensure_reasoning_stage_request(cycle_ref=graph.cycle_ref,
            accepted_question=bundle_request.accepted_question, idempotency_key="notes-reasoning-request")
        assert requested.context_pack["upstream_stage_closure"][-1]["closure"]["notes"] == LATE_NOTE
        from meta_research.reasoning_stage import _frozen_evidence_closure
        downstream = tmp_path / "actual-reasoning"
        downstream.mkdir()
        runner = reasoning_fixtures._SequenceRunner([reasoning_fixtures._stage_output()])
        adapter = CodexReasoningSkillAdapter(downstream / "provider", executable=str(reasoning_fixtures._fake_codex(downstream / "codex")), process_runner=runner)
        adapter.bind_full_conformance_authority(reasoning_fixtures._FullConformanceAuthority())
        adapter.configure_resident_mcp_endpoint("http://127.0.0.1:8765")
        request = replace(reasoning_fixtures._request(), runtime_binding=adapter.runtime_binding(),
            stage_request_ref=requested.request_ref, cycle_ref=requested.cycle_ref,
            question_ref=requested.accepted_question.question_ref, quest_ref=requested.accepted_question.quest_ref,
            foreground_epoch=requested.epoch, context_pack_ref=requested.context_pack_ref,
            context_pack_hash=requested.context_pack_hash, context_pack=requested.context_pack,
            goal_revision_ref=requested.context_pack["research_context"]["goal_revision_ref"],
            frozen_evidence_closure=_frozen_evidence_closure(requested.context_pack))
        # Observe delivery at the real provider boundary; this synthetic provider
        # draft is not submitted to any Owner.
        adapter.generate_draft(request)
        _context_preview(runner.calls[0][1], request.context_pack_hash)
        assert LATE_NOTE in runner.calls[0][1]
    finally:
        fixture.close()
