"""Bundle delivers exact Question content at the real adapter transport seam."""
import json
from dataclasses import replace

import pytest

from meta_research.bundle_skill import BundleSkillContractError, BundleSkillRequest, CodexBundleSkillAdapter
from meta_research.owners.common import canonical_hash
from test_bundle_skill_adapter import (
    _FullConformanceAuthority, _SequenceRunner, _fake_codex, _inbox_checkpoint,
    _plan_document, _target_plan,
)


def _question(optional_empty=False):
    return {
        "title": "区分形态和器件漂移",
        "unknown_statement": "器件变化如何影响形态判定？" * 260 + "问题末尾限定：只研究已选器件。",
        "answer_shape": "说明可识别的差异、负结果与不确定性。",
        "applicability_scope": "仅适用于已冻结的观察协议。",
        "background_context": "" if optional_empty else "背景原文 " * 500 + "背景末尾事实。",
        "requirements_constraints": "" if optional_empty else "约束原文 " * 500 + "约束末尾：不得删去负结果。",
    }


def _setup(tmp_path, question, runner_type=_SequenceRunner):
    plan = _plan_document()
    context = {
        "schema_ref": "meta-research/bundle-context-pack/v1",
        "cycle_ref": "cycle:bundle-1",
        "cycle_position": {"cycle_ref": "cycle:bundle-1", "round": 1,
            "cycle_focus": "Compare frozen observations", "focus_source": {"request_ref": "request:start"}},
        "accepted_question_binding": {"question_ref": "question:bundle-1",
            "content_ref": "question-content:original", "content_hash": canonical_hash(question)},
        "accepted_formal_plan_binding": {"formal_plan_ref": "formal-plan:bundle-1",
            "plan_document": plan, "plan_document_hash": canonical_hash(plan),
            "answer_contract_hash": "a" * 64},
    }
    runner = runner_type([{"target_plan": _target_plan(plan, canonical_hash(context))}] * 2)
    adapter = CodexBundleSkillAdapter(tmp_path / "provider",
        executable=str(_fake_codex(tmp_path / "codex")), process_runner=runner)
    adapter.bind_full_conformance_authority(_FullConformanceAuthority())
    adapter.configure_resident_mcp_endpoint("http://semantic-mcp.invalid")
    request = BundleSkillRequest(
        stage_request_ref="stage-request:bundle-1", run_ref="bundle-run:1",
        attempt_ref="bundle-attempt:1", fence_ref="bundle-fence:1",
        cycle_ref="cycle:bundle-1", question_ref="question:bundle-1",
        formal_plan_ref="formal-plan:bundle-1", context_pack_ref="context-pack:bundle-1",
        context_pack_hash=canonical_hash(context), context_pack=context,
        accepted_question_content=question, plan_document=plan,
        root_session_ref="ar-session:bundle-1", runtime_binding=adapter.runtime_binding(),
        inbox_checkpoint=_inbox_checkpoint(run_ref="bundle-run:1",
            attempt_ref="bundle-attempt:1", fence_ref="bundle-fence:1"),
    )
    return adapter, runner, request


@pytest.mark.parametrize("optional_empty", [False, True])
def test_bundle_first_provider_request_delivers_all_six_frozen_question_fields(tmp_path, optional_empty):
    expected = _question(optional_empty)
    adapter, runner, request = _setup(tmp_path, expected)
    adapter.generate_draft(request)
    prompt = runner.calls[0][1]
    delivered = [json.loads(line.removeprefix("accepted_question="))
        for line in prompt.splitlines() if line.startswith("accepted_question=")]
    assert delivered == [expected]
    view = json.loads(next(line.removeprefix("provider_context_view=")
        for line in prompt.splitlines() if line.startswith("provider_context_view=")))
    assert view["sections"]["accepted_question_binding"] == request.context_pack["accepted_question_binding"]
    assert view["reader"]["context_pack_ref"] == request.context_pack_ref


def test_bundle_rejection_resumes_original_root_without_repeating_question_or_plan(tmp_path):
    adapter, runner, request = _setup(tmp_path, _question())
    first = adapter.generate_draft(request)
    resumed = replace(request, native_session_ref=first.primary_session_ref,
        predecessor_candidate_ref="candidate:rejected",
        owner_rejection_receipt_ref="receipt:rejected", owner_rejection_kind="completion",
        owner_feedback=("Preserve the negative comparison in the revised TargetPlan.",))
    adapter.generate_draft(resumed)
    argv, prompt, _ = runner.calls[1]
    assert argv[-3:] == ["resume", first.primary_session_ref, "-"]
    assert "Preserve the negative comparison" in prompt
    assert "accepted_question=" not in prompt
    assert "provider_context_view=" not in prompt
    assert request.context_pack_ref in prompt
    assert request.context_pack_hash in prompt
    assert "research_memory.stage_context.read" in prompt


def test_bundle_rejects_question_body_outside_frozen_hash_before_provider_call(tmp_path):
    adapter, runner, request = _setup(tmp_path, _question())
    wrong = {**request.accepted_question_content, "title": "A different Question"}
    with pytest.raises(BundleSkillContractError, match="accepted_question_content_mismatch"):
        adapter.generate_draft(replace(request, accepted_question_content=wrong))
    assert runner.calls == []


def test_bundle_replay_keeps_original_operation_and_question_bytes(tmp_path):
    from bundle_signed_runner import _SignedDispatchRunner
    adapter, runner, request = _setup(tmp_path, _question(), _SignedDispatchRunner)
    request = replace(request, job_ref="bundle:first-packet")
    first = adapter.generate_draft(request)
    operation = tmp_path / "provider" / "provider-operations" / canonical_hash({"job_ref": request.job_ref}) / "primary"
    sealed = {path.name: path.read_bytes() for path in operation.iterdir() if path.is_file()}
    reordered = replace(request, accepted_question_content=dict(reversed(list(request.accepted_question_content.items()))))
    assert adapter.generate_draft(reordered) == first
    assert len(runner.calls) == 1
    assert {path.name: path.read_bytes() for path in operation.iterdir() if path.is_file()} == sealed


def test_bundle_stage_resolves_frozen_question_before_real_adapter_delivery(tmp_path):
    from test_human_guidance_providers import _tool
    from test_public_bundle_stage import _bundle_runtime, _prepare_bundle_request
    from test_public_plan_stage import _QUESTION
    from bundle_signed_runner import _SignedDispatchRunner

    class Delivered(BaseException):
        pass

    observed = []

    def runner(argv, prompt, timeout, environment=None):
        delivered = json.loads(next(line.removeprefix("accepted_question=")
            for line in prompt.splitlines() if line.startswith("accepted_question=")))
        exact = _tool(runtime, environment["META_RESEARCH_MCP_TOKEN"],
            "research_memory.stage_context.read", context_pack_ref=request.context_pack_ref,
            source="question", path=[], offset=0, limit=16384)
        observed.append(delivered)
        assert delivered == _QUESTION
        assert json.loads(exact["text"]) == delivered
        assert exact["source_ref"] == request.accepted_question.content_ref
        assert canonical_hash(delivered) == request.accepted_question.content_hash
        raise Delivered()

    class ProbeRunner(_SignedDispatchRunner):
        def __call__(self, *args, **kwargs):
            return runner(*args, **kwargs)

    adapter = CodexBundleSkillAdapter(tmp_path / "provider",
        executable=str(_fake_codex(tmp_path / "codex")), process_runner=ProbeRunner([]))
    runtime = _bundle_runtime(tmp_path / "runtime", bundle_skill_provider=adapter)
    runtime.configure_resident_mcp_endpoint("http://127.0.0.1:8765")
    runtime.bundle_stage.configure_resident_mcp_endpoint("http://127.0.0.1:8765")
    try:
        _prepare_bundle_request(runtime)
        current = runtime.bundle_stage.query_current()["stage_run_request"]
        request = runtime.owners.advancement_engine.query_bundle_stage_request(current["cycle_ref"])
        with pytest.raises(Delivered):
            for _ in range(4):
                assert runtime.bundle_stage.process_once(), runtime.bundle_stage.observation.code
        assert observed == [_QUESTION]
    finally:
        runtime.close()
