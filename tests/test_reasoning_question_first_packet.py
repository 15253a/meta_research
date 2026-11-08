"""Observe complete frozen science at the production adapter's process boundary."""
import json
from dataclasses import replace

import pytest

from meta_research.reasoning_skill import CodexReasoningSkillAdapter
from meta_research.owners.common import canonical_hash
from test_reasoning_skill_adapter import (
    _FullConformanceAuthority, _SequenceRunner, _fake_codex, _request, _stage_output,
)


@pytest.mark.parametrize("empty_supplements", [False, True])
def test_reasoning_first_packet_delivers_question_then_resumes_with_only_delta(tmp_path, empty_supplements):
    question = {
        "title": "Compare competing explanations",
        "unknown_statement": "Which explanation survives the negative control?",
        "answer_shape": "A bounded comparison with uncertainty",
        "applicability_scope": "Only the frozen cohort",
        "background_context": "" if empty_supplements else "背景" * 5000 + "背景尾部",
        "requirements_constraints": "" if empty_supplements else "约束" * 5000 + "不得推广到新群体",
    }
    runner = _SequenceRunner([_stage_output(), _stage_output()])
    adapter = CodexReasoningSkillAdapter(tmp_path / "provider", executable=str(_fake_codex(tmp_path / "codex")), process_runner=runner)
    adapter.bind_full_conformance_authority(_FullConformanceAuthority())
    adapter.configure_resident_mcp_endpoint("http://semantic-mcp.invalid")
    context = _request().context_pack
    context["accepted_question_binding"]["content_hash"] = canonical_hash(question)
    request = replace(_request(), runtime_binding=adapter.runtime_binding(), accepted_question_content=question,
                      context_pack=context, context_pack_hash=canonical_hash(context))
    first = adapter.generate_draft(request)
    prompt = runner.calls[0][1]
    delivered = json.loads(next(line.removeprefix("accepted_question=") for line in prompt.splitlines() if line.startswith("accepted_question=")))
    assert delivered == question
    assert "source=literature_records" in prompt
    resumed = replace(request, native_session_ref=first.primary_session_ref,
                      predecessor_candidate_ref="rejected:1", owner_rejection_receipt_ref="rejection:1",
                      owner_rejection_kind="domain", owner_feedback=("Explain the negative control.",))
    adapter.generate_draft(resumed)
    assert runner.calls[1][0][-3:] == ["resume", first.primary_session_ref, "-"]
    delta = runner.calls[1][1]
    assert "Explain the negative control." in delta
    assert "accepted_question=" not in delta
    assert "provider_context_view=" not in delta
    assert "context_pack_ref=" in delta


def test_reasoning_worker_delivers_the_frozen_question_to_the_actual_provider(tmp_path):
    from test_public_reasoning_stage import (
        _reasoning_runtime, _confirm_deepfetch_quest, _finish_idea_stage,
    )
    from test_human_guidance_providers import _GuidanceRunner, _tool

    class Delivered(BaseException):
        pass

    class Capture(_GuidanceRunner):
        def __call__(self, argv, prompt, timeout, environment=None):
            self.prompt = prompt
            frozen = runtime.owners.advancement_engine.query_reasoning_stage_request(str(quest["cycle_ref"]))
            self.exact = _tool(runtime, environment["META_RESEARCH_MCP_TOKEN"],
                               "research_memory.stage_context.read", context_pack_ref=frozen.context_pack_ref,
                               source="question", path=[], offset=0, limit=16384)
            raise Delivered()

    runner = Capture()
    adapter = CodexReasoningSkillAdapter(tmp_path / "provider", executable=str(_fake_codex(tmp_path / "codex")), process_runner=runner)
    runtime = _reasoning_runtime(tmp_path / "runtime", reasoning_skill=adapter)
    runtime.configure_resident_mcp_endpoint("http://127.0.0.1:8999")
    runtime.reasoning_stage.configure_resident_mcp_endpoint("http://127.0.0.1:8999")
    try:
        quest = _confirm_deepfetch_quest(runtime)
        _finish_idea_stage(runtime)
        with pytest.raises(Delivered):
            for _ in range(8):
                assert runtime.reasoning_stage.process_once(), runtime.reasoning_stage.transient_error
        request = runtime.owners.advancement_engine.query_reasoning_stage_request(str(quest["cycle_ref"]))
        binding = request.accepted_question
        expected = runtime.owners.research_memory.read_question_content(binding.content_ref, binding.content_hash)
        actual = json.loads(next(line.removeprefix("accepted_question=") for line in runner.prompt.splitlines() if line.startswith("accepted_question=")))
        assert actual == expected
        assert json.loads(runner.exact["text"]) == actual
        assert runner.exact["source_ref"] == binding.content_ref
        assert canonical_hash(actual) == binding.content_hash
        assert binding.content_ref in runner.prompt
    finally:
        runtime.close()
