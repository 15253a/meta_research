"""Observe Cycle role methods and real reference reads at provider delivery."""
from dataclasses import replace
from pathlib import Path
import json
import re

from meta_research.bundle_skill import BundleSkillRequest, CodexBundleSkillAdapter
from meta_research.owners.common import canonical_hash, canonical_json
from test_bundle_skill_adapter import _question_content
from test_bundle_skill_adapter import (
    _FullConformanceAuthority, _SequenceRunner, _fake_codex,
    _inbox_checkpoint, _plan_document, _target_plan,
)
from test_public_guidance_delivery import _developer


def _read_role(argv, user, role, title, references):
    developer = _developer(argv)
    assert title in developer
    assert title not in user
    root = Path(re.search(r"^Shared reference root: (.+)$", developer, re.MULTILINE).group(1))
    cwd = Path(argv[argv.index("--cd") + 1])
    assert cwd.is_dir()
    source = root / role / "SKILL.md"
    assert str(source) in user or str(source) in developer
    skill = source.read_text(encoding="utf-8")
    for name in references:
        assert name in skill
        body = (source.parent / name).read_text(encoding="utf-8")
        assert body and body not in user and body not in developer
        for link in re.findall(r"\]\(([^)]+)\)", body):
            relative = link.split("#", 1)[0]
            if relative and "://" not in relative:
                assert (source.parent / Path(name).parent / relative).resolve().is_file(), link
    return developer


def test_bundle_initial_and_rejection_keep_methods_resident_and_references_readable(tmp_path):
    plan = _plan_document()
    context = {
        "schema_ref": "meta-research/bundle-context-pack/v1", "cycle_ref": "cycle:bundle-1",
        "cycle_position": {"cycle_ref": "cycle:bundle-1", "round": 1, "cycle_focus": "Audit frozen scientific inputs", "focus_source": {"request_ref": "fixture-start"}},
        "accepted_question_binding": {"question_ref": "question:bundle-1", "content_hash": canonical_hash(_question_content())},
        "accepted_formal_plan_binding": {"formal_plan_ref": "formal-plan:bundle-1", "plan_document": plan, "plan_document_hash": canonical_hash(plan), "answer_contract_hash": "a" * 64},
    }
    delegate = _SequenceRunner([{"target_plan": _target_plan(plan, canonical_hash(context))}] * 2)
    observed = []

    def runner(argv, prompt, timeout, environment=None):
        observed.append(_read_role(argv, prompt, "bundle_stage", "# Bundle：组织实际研究", ("references/contract.md", "references/owner-operations.md")))
        assert (canonical_json(plan) in prompt) == (len(observed) == 1)
        return delegate(argv, prompt, timeout, environment)

    adapter = CodexBundleSkillAdapter(tmp_path / "provider", executable=str(_fake_codex(tmp_path / "codex")), process_runner=runner)
    adapter.bind_full_conformance_authority(_FullConformanceAuthority())
    adapter.configure_resident_mcp_endpoint("http://127.0.0.1:8765")
    request = BundleSkillRequest(
        accepted_question_content=_question_content(),
        stage_request_ref="stage-request:bundle-1", run_ref="bundle-run:1", attempt_ref="bundle-attempt:1",
        fence_ref="bundle-fence:1", cycle_ref="cycle:bundle-1", question_ref="question:bundle-1",
        formal_plan_ref="formal-plan:bundle-1", context_pack_ref="context-pack:bundle-1",
        context_pack_hash=canonical_hash(context), context_pack=context, plan_document=plan,
        root_session_ref="ar-session:bundle-1", runtime_binding=adapter.runtime_binding(),
        inbox_checkpoint=_inbox_checkpoint(run_ref="bundle-run:1", attempt_ref="bundle-attempt:1", fence_ref="bundle-fence:1"),
    )
    initial = adapter.execute(request)
    revised = replace(request, attempt_ref="bundle-attempt:2", fence_ref="bundle-fence:2",
        inbox_checkpoint=_inbox_checkpoint(run_ref="bundle-run:1", attempt_ref="bundle-attempt:2", fence_ref="bundle-fence:2"),
        native_session_ref=initial.primary_session_ref, predecessor_candidate_ref="bundle-candidate:rejected",
        owner_rejection_receipt_ref="receipt:bundle-completion-rejected", owner_rejection_kind="completion",
        owner_feedback=("修正 TargetPlan 的 reference closure。",))
    result = adapter.execute(revised)
    assert result.primary_session_ref == initial.primary_session_ref
    assert observed[0] == observed[1]
    assert delegate.calls[1][0][-3:] == ["resume", initial.primary_session_ref, "-"]
    assert "修正 TargetPlan 的 reference closure。" in delegate.calls[1][1]


def test_target_actual_launch_delivers_method_and_exact_owner_science(tmp_path):
    from meta_research.harness_adapters import CodexHarnessAdapter
    from test_harness_adapters import _RecordedRunner
    from test_target_root_finalizer import _current_bundle_runtime
    from test_target_launch_admission import _ready_launch

    class Delivered(BaseException):
        pass

    observed = []
    probes = _RecordedRunner("codex", ())

    def runner(argv, prompt, timeout, environment):
        if "--cd" not in argv:
            return probes(argv, prompt, timeout, environment)
        _read_role(argv, prompt, "target-execution", "# 实施已接纳的 Target", ("references/formal-work.md", "references/data-preservation.md"))
        material, _ = json.JSONDecoder().raw_decode(prompt.split("Exact Owner context:\n", 1)[1])
        observed.append(material)
        assert material["execution_contract"]["measurement_contract"]
        assert Path(material["frozen_input_manifest_path"]).is_file()
        raise Delivered()

    runtime = _current_bundle_runtime(tmp_path / "runtime", harness_adapters=(CodexHarnessAdapter(tmp_path / "harness", runner=runner),))
    try:
        graph, target, _bundle, dispatch, launch = _ready_launch(runtime)
        runtime.owners.agent_runtime.admit_target_launch(launch, dispatch_decision_ref=dispatch.decision_ref, idempotency_key="role-delivery-target")
        runtime.configure_resident_mcp_endpoint("http://127.0.0.1:8765")
        runtime.target_run_runtime.configure_resident_mcp_endpoint("http://127.0.0.1:8765")
        for _ in range(16):
            try:
                runtime.target_run_runtime.process_once(target.target_ref)
            except Delivered:
                break
        assert len(observed) == 1
        assert observed[0]["research_context"]["graph_ref"] == graph.graph_ref
        original = runtime.target_run_authorities.research_graph.query_target_research_context(target_ref=target.target_ref)
        for field in ("question", "plan_notes", "bundle_notes"):
            assert observed[0]["research_context"][field] == original[field]
    finally:
        runtime.close()
