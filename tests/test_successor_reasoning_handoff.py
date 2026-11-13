"""Accepted predecessor science survives each successor entry and later stages."""

from copy import deepcopy
from dataclasses import replace
import json
from types import SimpleNamespace

import pytest

from meta_research.context_presentation import (
    CONTEXT_VIEW_MAX_BYTES, reasoning_handoff_reference, stage_context_view,
)
from meta_research.owners.common import OwnerConflict, canonical_hash, canonical_json
from meta_research.plan_contract import validate_plan_context_pack
from meta_research.semantic_mcp import SemanticMcpError
from meta_research.stage_context_access import read_stage_context
from test_public_reasoning_stage import (
    _DeterministicReasoningSkill,
    _MultiRunExhaustionBundleSkill,
    _MultiRunIdeaSkill,
    _MultiRunPlanSkill,
    _confirm_deepfetch_quest,
    _finish_idea_stage,
    _reasoning_runtime,
)


NOTES = "上一轮的完整交接：保留不确定性、反例和未完成观察。α\n" * 1300 + "备注末尾：先核实缺失样本。"
LIMITATIONS = [f"边界 {index}：" + "独立队列尚未验证；" * 80 for index in range(20)]


class _MarkedReasoningSkill(_DeterministicReasoningSkill):
    def _result_parts(self, request):
        outcome, proposal = super()._result_parts(request)
        outcome["notes"] = NOTES
        outcome["limitations"] = LIMITATIONS
        if self.entry_stage == "plan":
            idea = next(item for item in request.context_pack["upstream_stage_closure"]
                        if item["stage"] == "idea")
            proposal["typed_skip_basis_refs_by_stage"] = {"idea": [idea["outcome_ref"]]}
        return outcome, proposal


def _finish_stage(runtime, stage, cycle_ref):
    owner = runtime.owners.advancement_engine
    processor = getattr(runtime, stage + "_stage")
    for _step in range(30):
        request = getattr(owner, "query_" + stage + "_stage_request")(cycle_ref)
        commit = None if request is None else getattr(owner, "query_" + stage + "_stage_commit")(request.request_ref)
        if commit is not None:
            return commit
        assert processor.process_once(), (
            getattr(processor, "transient_error", None),
            getattr(processor, "observation", None),
        )
    raise AssertionError(f"{stage} did not commit")


def _start_stage(runtime, stage, cycle_ref):
    owner = runtime.owners.advancement_engine
    processor = getattr(runtime, stage + "_stage")
    for _step in range(4):
        request = getattr(owner, "query_" + stage + "_stage_request")(cycle_ref)
        run = None if request is None else getattr(runtime.owners.agent_runtime, "query_" + stage + "_stage_run")(request.request_ref)
        if run is not None:
            return request, SimpleNamespace(
                root_kind=stage, run_ref=run.run_ref, attempt_ref=run.attempt_ref,
                root_session_ref=run.root_session_ref, fence_ref=run.fence_ref,
                capability_binding_hash=run.runtime_binding_hash,
            )
        assert processor.process_once(), processor.transient_error
    raise AssertionError(f"{stage} did not admit its root")


def _read(runtime, context, arguments):
    return read_stage_context(
        runtime.owners.advancement_engine, runtime.owners.agent_runtime,
        runtime.owners.research_graph, runtime.owners.research_memory,
        context, arguments,
    )


def _read_exact(runtime, context, arguments, expected):
    arguments = {"path": [], "offset": 0, "limit": 16381, **arguments}
    chunks = []
    while True:
        page = _read(runtime, context, arguments)
        assert page["content_hash"] == canonical_hash(expected)
        chunks.append(page["text"])
        if page["next_offset"] is None:
            break
        arguments["offset"] = page["next_offset"]
    assert "".join(chunks) == canonical_json(expected)
    assert json.loads("".join(chunks)) == expected


def _assert_handoff(runtime, stage, cycle_ref, source):
    request, context = _start_stage(runtime, stage, cycle_ref)
    pack_before = deepcopy(request.context_pack)
    prior, = request.context_pack.get("prior_accepted_bindings", [])
    assert prior["cycle_ref"] == source.cycle_ref
    assert prior["commit_ref"] == source.commit_ref
    assert prior["outcome_ref"] == source.outcome_ref
    assert prior["closure_hash"] == canonical_hash(source.closure)
    assert prior["scientific_summary"] == source.closure["scientific_summary"]
    assert prior["handoff_notes"] == {
        "text": NOTES, "source_utf8_bytes": len(NOTES.encode()), "truncated": False,
    }
    if stage == "plan":
        owner = runtime.owners.advancement_engine
        forged = deepcopy(request.context_pack)
        forged["prior_accepted_bindings"][0]["handoff_notes"]["text"] = "Caller-authored replacement"
        for index, value in enumerate((forged["prior_accepted_bindings"], [])):
            with pytest.raises(OwnerConflict, match="plan_reasoning_handoff_invalid"):
                owner.ensure_plan_stage_request(
                    cycle_ref=cycle_ref, accepted_question=request.accepted_question,
                    accepted_idea_set=request.accepted_idea_set,
                    context_pack={**request.context_pack, "prior_accepted_bindings": value},
                    idempotency_key=f"forged-prior-{index}",
                )
        # Previous Plan context versions remain valid without rewriting them.
        legacy = deepcopy(request.context_pack)
        del legacy["prior_accepted_bindings"]
        validate_plan_context_pack(legacy, cycle_ref=cycle_ref,
            accepted_question_binding=request.accepted_question.as_dict())
    view = stage_context_view(stage, request.context_pack,
        context_pack_ref=request.context_pack_ref, context_pack_hash=request.context_pack_hash)
    assert view["section_delivery"]["prior_accepted_bindings"] == "complete"
    assert view["sections"]["prior_accepted_bindings"] == [prior]
    assert request.context_pack == pack_before
    assert canonical_hash(request.context_pack) == request.context_pack_hash

    base = {"context_pack_ref": request.context_pack_ref}
    _read_exact(runtime, context,
        {**base, "source": "predecessor_closure", "source_ref": source.commit_ref},
        source.closure)
    scientific = {**source.closure["scientific_summary"], "notes": NOTES}
    _read_exact(runtime, context,
        {**base, "source": "scientific_outcome", "source_ref": source.outcome_ref}, scientific)
    pointer, = view["predecessor_research_notes_readers"]
    assert pointer["predecessor_ref"] == source.commit_ref
    args = {key: value for key, value in pointer.items() if key != "operation"}
    notes = runtime.owners.research_memory.query_question_research_notes(
        quest_ref=request.accepted_question.quest_ref,
        question_ref=source.closure["transition"]["source_question_ref"], offset=0)
    _read_exact(runtime, context, args, notes)
    with pytest.raises(SemanticMcpError, match="research_note_predecessor_unbound"):
        _read(runtime, context, {**args, "predecessor_ref": "unaccepted-predecessor"})
    with pytest.raises(SemanticMcpError, match="stage_predecessor_source_unbound"):
        _read(runtime, context, {**base, "source": "predecessor_closure",
                                "source_ref": "unaccepted-predecessor"})
    context.fence_ref = "stale-fence"
    with pytest.raises(SemanticMcpError, match="semantic_call_scope_stale"):
        _read(runtime, context, args)


@pytest.mark.parametrize("entry_stage", ["idea", "plan", "reasoning"])
def test_complete_accepted_reasoning_reaches_successor_and_later_stages(tmp_path, entry_stage):
    assert len(NOTES.encode()) > CONTEXT_VIEW_MAX_BYTES
    runtime = _reasoning_runtime(
        tmp_path / entry_stage,
        reasoning_skill=_MarkedReasoningSkill(entry_stage=entry_stage),
        idea_skill=_MultiRunIdeaSkill(), plan_skill=_MultiRunPlanSkill(no_gap=False),
        bundle_skill=_MultiRunExhaustionBundleSkill(),
    )
    try:
        quest = _confirm_deepfetch_quest(runtime)
        _finish_idea_stage(runtime)
        _finish_stage(runtime, "plan", quest["cycle_ref"])
        _finish_stage(runtime, "bundle", quest["cycle_ref"])
        source = _finish_stage(runtime, "reasoning", quest["cycle_ref"])
        assert source.disposition == "completed"
        assert source.closure["notes"] == NOTES
        assert source.closure["scientific_summary"]["limitations"] == LIMITATIONS
        owner = runtime.owners.advancement_engine
        successor = owner.query_foreground(quest["quest_ref"])
        assert successor["cycle_ref"] != source.cycle_ref
        assert successor["stage"] == entry_stage
        cycle_ref = successor["cycle_ref"]
        if entry_stage == "idea":
            _assert_handoff(runtime, "idea", cycle_ref, source)
            _finish_stage(runtime, "idea", cycle_ref)
        if entry_stage in {"idea", "plan"}:
            _assert_handoff(runtime, "plan", cycle_ref, source)
            _finish_stage(runtime, "plan", cycle_ref)
            _finish_stage(runtime, "bundle", cycle_ref)
        _assert_handoff(runtime, "reasoning", cycle_ref, source)
    finally:
        runtime.close()


def test_reasoning_request_frozen_before_handoff_extension_stays_readable(tmp_path, monkeypatch):
    from meta_research.owners import advancement_engine

    runtime = _reasoning_runtime(tmp_path / "legacy", reasoning_skill=_MarkedReasoningSkill())
    try:
        quest = _confirm_deepfetch_quest(runtime)
        _finish_idea_stage(runtime)
        build_context = advancement_engine._reasoning_context_pack_from_rows

        def historical_context(*args, **kwargs):
            # Emulate the previous producer at the construction boundary, so
            # Owner storage still creates all hashes and receipts normally.
            pack = build_context(*args, **kwargs)
            pack.pop("prior_accepted_bindings", None)
            return pack

        with monkeypatch.context() as historical:
            historical.setattr(advancement_engine, "_reasoning_context_pack_from_rows", historical_context)
            assert runtime.reasoning_stage.process_once()
            frozen = runtime.owners.advancement_engine.query_reasoning_stage_request(quest["cycle_ref"])
            assert frozen is not None and "prior_accepted_bindings" not in frozen.context_pack

        reread = runtime.owners.advancement_engine.query_reasoning_stage_request(quest["cycle_ref"])
        assert reread == frozen
        admitted, _ = _start_stage(runtime, "reasoning", quest["cycle_ref"])
        assert admitted == frozen
        assert canonical_hash(admitted.context_pack) == frozen.context_pack_hash
    finally:
        runtime.close()


@pytest.mark.parametrize("stage", ["plan", "reasoning"])
def test_actual_primary_prompt_keeps_complete_predecessor_science_and_notes(tmp_path, stage):
    from meta_research.plan_skill import CodexPlanSkillAdapter
    from meta_research.reasoning_skill import CodexReasoningSkillAdapter
    import test_plan_skill_adapter as plan_fixtures
    import test_reasoning_skill_adapter as reasoning_fixtures

    scientific = deepcopy(reasoning_fixtures._stage_output()["scientific_outcome"])
    scientific.pop("notes")
    scientific.update(outcome_ref="outcome:previous", cycle_ref="cycle:previous",
                      stage_run_request_ref="stage-request:previous")
    scientific["research_synthesis"]["cycle"]["cycle_ref"] = "cycle:previous"
    scientific["limitations"] = LIMITATIONS
    prior = reasoning_handoff_reference({
        "cycle_ref": "cycle:previous", "commit_ref": "commit:previous",
        "outcome_ref": "outcome:previous",
        "receipt": plan_fixtures._receipt("advancement_engine", "stage_commit",
            "receipt:previous-commit", "commit:previous", "a"),
        "outcome_receipt": plan_fixtures._receipt("research_graph", "reasoning_outcome_accepted",
            "receipt:previous-outcome", "outcome:previous", "b"),
        "closure": {"notes": NOTES, "scientific_summary": scientific},
    })
    if stage == "plan":
        request = plan_fixtures._request()
        output = {"plan": plan_fixtures._plan()}
        runner = plan_fixtures._SequenceRunner([output])
        adapter = CodexPlanSkillAdapter(tmp_path / "provider",
            executable=str(plan_fixtures._fake_codex(tmp_path / "codex")), process_runner=runner)
    else:
        request = reasoning_fixtures._request()
        output = reasoning_fixtures._stage_output()
        runner = reasoning_fixtures._SequenceRunner([output])
        adapter = CodexReasoningSkillAdapter(tmp_path / "provider",
            executable=str(reasoning_fixtures._fake_codex(tmp_path / "codex")), process_runner=runner)
        adapter.bind_full_conformance_authority(reasoning_fixtures._FullConformanceAuthority())
        adapter.configure_resident_mcp_endpoint("http://127.0.0.1:8765")
    pack = {**deepcopy(request.context_pack), "prior_accepted_bindings": [prior]}
    request = replace(request, runtime_binding=adapter.runtime_binding(),
        context_pack=pack, context_pack_hash=canonical_hash(pack))
    if stage == "plan":
        output["plan"]["source_bindings"]["context_pack_hash"] = request.context_pack_hash
    adapter.generate_draft(request)
    prompt = runner.calls[0][1]
    line, = [line for line in prompt.splitlines() if line.startswith("provider_context_view=")]
    view = json.loads(line.split("=", 1)[1])
    assert view["source_context_pack_hash"] == request.context_pack_hash
    assert view["section_delivery"]["prior_accepted_bindings"] == "complete"
    assert view["sections"]["prior_accepted_bindings"] == [prior]
    assert view["sections"]["prior_accepted_bindings"][0]["handoff_notes"]["text"] == NOTES
    assert view["sections"]["prior_accepted_bindings"][0]["scientific_summary"] == scientific
