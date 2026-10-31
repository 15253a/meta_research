"""Observe packaged, on-demand references at the actual provider boundary."""
from pathlib import Path
from dataclasses import replace
import hashlib
import json
import re
import tomllib

import pytest

from meta_research.plan_skill import CodexPlanSkillAdapter
from test_plan_skill_adapter import _fake_codex, _plan, _request, _SequenceRunner


SHARED_NAMES = (
    "research-guidance.md", "human-guidance.md", "research-navigation.md",
    "cycle-work.md", "materials-and-custody.md",
)


def _developer(argv):
    configs = [argv[i + 1] for i, value in enumerate(argv[:-1]) if value == "--config"]
    return tomllib.loads(next(value for value in configs if value.startswith("developer_instructions=")))["developer_instructions"]


class _ReadingRunner:
    def __init__(self, delegate, names=SHARED_NAMES):
        self.delegate = delegate
        self.names = names
        self.observed = []

    def __call__(self, argv, prompt, timeout, environment=None):
        developer = _developer(argv)
        root_match = re.search(r"^Shared reference root: (.+)$", developer, re.MULTILINE)
        assert root_match, "provider needs an explicit, readable packaged resource root"
        root = Path(root_match.group(1))
        cwd = Path(argv[argv.index("--cd") + 1])
        assert root.is_absolute() and cwd.is_dir()
        documents = {name: (root / "references" / name).read_text(encoding="utf-8") for name in self.names}
        for name, body in documents.items():
            assert f"references/{name}" in developer
            assert body.strip() and body not in developer and body not in prompt
            for target in re.findall(r"\]\(([^)]+)\)", body):
                relative = target.split("#", 1)[0]
                if relative and "://" not in relative:
                    linked = (root / "references" / relative).resolve()
                    assert linked.is_relative_to(root) and linked.is_file(), target
        evidence = {
            "cwd": str(cwd), "shared_reference_root": str(root),
            "delivery_channels": {"developer": developer, "user": prompt},
            "references_read": {name: {"utf8_bytes": len(body.encode()), "sha256": hashlib.sha256(body.encode()).hexdigest()} for name, body in documents.items()},
        }
        (cwd / f"public-guidance-observation-{len(self.observed)}.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
        self.observed.append((cwd, developer, documents))
        return self.delegate(argv, prompt, timeout, environment)

    @property
    def calls(self):
        return self.delegate.calls


def test_plan_provider_can_read_shared_references_without_inline_long_bodies(tmp_path):
    runner = _ReadingRunner(_SequenceRunner([{"plan": _plan()}]))
    adapter = CodexPlanSkillAdapter(tmp_path / "provider", executable=str(_fake_codex(tmp_path / "codex")), process_runner=runner)
    request = _request(runtime_binding=adapter.runtime_binding())

    result = adapter.generate_draft(request)

    assert result.primary_session_ref == "codex-plan-primary:1"
    argv, user, schema = runner.calls[0]
    assert "哪种约束保留稀有形态？" in user
    assert "比较拓扑一致性与像素重建。" in user
    assert "selected_evidence_bindings" in user
    assert "answer_contract" in schema["properties"]["plan"]["properties"]
    assert "mcp_servers={}" in argv


def test_plan_revision_keeps_readable_references_and_exact_scientific_increment(tmp_path):
    runner = _ReadingRunner(_SequenceRunner([{"plan": _plan()}, {"plan": _plan("Corrected device comparison")}]))
    adapter = CodexPlanSkillAdapter(tmp_path / "provider", executable=str(_fake_codex(tmp_path / "codex")), process_runner=runner)
    request = _request(runtime_binding=adapter.runtime_binding())
    first = adapter.generate_draft(request)
    revision = replace(request, native_session_ref=first.primary_session_ref,
        submission_revision=2, predecessor_submission_ref="plan-submission:1",
        owner_rejection_receipt_ref="receipt:rejection:1", owner_rejection_kind="domain",
        owner_feedback=("Correct the device comparison, preserve the frozen IdeaSet.",))

    result = adapter.generate_draft(revision)

    assert result.primary_session_ref == first.primary_session_ref
    argv, user, schema = runner.calls[1]
    assert argv[-3:] == ["resume", first.primary_session_ref, "-"]
    assert runner.observed[1] == runner.observed[0]
    assert "Correct the device comparison, preserve the frozen IdeaSet." in user
    assert "哪种约束保留稀有形态？" in user and "比较拓扑一致性与像素重建。" in user
    assert schema == runner.calls[0][2]


def test_idea_provider_reads_shared_references_at_initial_delivery(tmp_path):
    from meta_research.idea_skill import CodexIdeaSkillAdapter
    from test_idea_skill_contract import _request as idea_request, _idea_set, _SequenceRunner as IdeaRunner
    runner = _ReadingRunner(IdeaRunner([{"outcome": _idea_set()}]))
    adapter = CodexIdeaSkillAdapter(tmp_path / "provider", executable=str(_fake_codex(tmp_path / "codex")), process_runner=runner)
    request = idea_request(runtime_binding=adapter.runtime_binding())
    result = adapter.generate_draft(request)
    assert result.primary_session_ref == "codex-primary:1"
    assert "accepted_question=" in runner.calls[0][1]
    assert set(runner.calls[0][2]["properties"]) == {"outcome"}


def test_reasoning_provider_reads_shared_references_with_resident_authority(tmp_path):
    from meta_research.reasoning_skill import CodexReasoningSkillAdapter
    from test_reasoning_skill_adapter import _request as reasoning_request, _stage_output, _SequenceRunner as ReasoningRunner, _FullConformanceAuthority
    runner = _ReadingRunner(ReasoningRunner([_stage_output()]))
    adapter = CodexReasoningSkillAdapter(tmp_path / "provider", executable=str(_fake_codex(tmp_path / "codex")), process_runner=runner)
    authority = _FullConformanceAuthority()
    adapter.bind_full_conformance_authority(authority)
    adapter.configure_resident_mcp_endpoint("http://127.0.0.1:8765")
    result = adapter.generate_draft(replace(reasoning_request(), runtime_binding=adapter.runtime_binding()))
    assert result.primary_session_ref == "provider-session:1"
    assert len(authority.issued) == len(authority.revoked) == 1
    assert runner.calls[0][2]["required"] == ["provider_output"]


def test_bundle_provider_reads_shared_references_with_complete_formal_plan(tmp_path):
    from meta_research.bundle_skill import CodexBundleSkillAdapter, BundleSkillRequest
    from meta_research.owners.common import canonical_hash, canonical_json
    from test_bundle_skill_adapter import _plan_document, _target_plan, _inbox_checkpoint, _FullConformanceAuthority, _SequenceRunner as BundleRunner
    plan = _plan_document()
    context = {
        "schema_ref": "meta-research/bundle-context-pack/v1", "cycle_ref": "cycle:bundle-1",
        "cycle_position": {"cycle_ref": "cycle:bundle-1", "round": 1, "cycle_focus": "Audit frozen scientific inputs", "focus_source": {"request_ref": "fixture-start"}},
        "accepted_question_binding": {"question_ref": "question:bundle-1"},
        "accepted_formal_plan_binding": {"formal_plan_ref": "formal-plan:bundle-1", "plan_document": plan, "plan_document_hash": canonical_hash(plan), "answer_contract_hash": "a" * 64},
    }
    context_hash = canonical_hash(context)
    runner = _ReadingRunner(BundleRunner([{"target_plan": _target_plan(plan, context_hash)}]))
    adapter = CodexBundleSkillAdapter(tmp_path / "provider", executable=str(_fake_codex(tmp_path / "codex")), process_runner=runner)
    authority = _FullConformanceAuthority()
    adapter.bind_full_conformance_authority(authority)
    adapter.configure_resident_mcp_endpoint("http://127.0.0.1:8765")
    request = BundleSkillRequest(
        stage_request_ref="stage-request:bundle-1", run_ref="bundle-run:1", attempt_ref="bundle-attempt:1",
        fence_ref="bundle-fence:1", cycle_ref="cycle:bundle-1", question_ref="question:bundle-1",
        formal_plan_ref="formal-plan:bundle-1", context_pack_ref="context-pack:bundle-1",
        context_pack_hash=context_hash, context_pack=context, plan_document=plan,
        root_session_ref="ar-session:bundle-1", runtime_binding=adapter.runtime_binding(),
        inbox_checkpoint=_inbox_checkpoint(run_ref="bundle-run:1", attempt_ref="bundle-attempt:1", fence_ref="bundle-fence:1"),
    )
    adapter.execute(request)
    assert canonical_json(plan) in runner.calls[0][1]
    assert len(authority.issued) == len(authority.revoked) == 1


@pytest.mark.parametrize("root_kind", ("idea", "plan", "bundle", "reasoning", "target", "deepfetch", "acquisition", "companion", "writing"))
@pytest.mark.parametrize("entry_path", ("initial", "resume"))
def test_harness_maintains_role_scoped_reference_access(tmp_path, root_kind, entry_path):
    from meta_research.codex_runtime import CODEX_MODEL_REF
    from meta_research.harness_adapters import CodexHarnessAdapter, HarnessInvocation
    from meta_research.root_resident_mcp import RootResidentMcpChannels
    from test_root_capability_floor import _NoActivityCodexRunner
    names = ("human-guidance.md", "materials-and-custody.md") if root_kind == "acquisition" else tuple(
        name for name in SHARED_NAMES if name != "cycle-work.md" or root_kind in {"idea", "plan", "bundle", "target", "reasoning"})
    session = f"session:{root_kind}"
    reading = _ReadingRunner(_NoActivityCodexRunner(session), names)

    # Version/inventory queries are provider probes, not model deliveries.
    def runner(argv, prompt, timeout, environment):
        if "--cd" not in argv:
            return reading.delegate(argv, prompt, timeout, environment)
        return reading(argv, prompt, timeout, environment)

    operation_ids = RootResidentMcpChannels(root_kind).operation_ids
    result = CodexHarnessAdapter(tmp_path / root_kind, runner=runner).invoke(HarnessInvocation(
        harness_family="codex", provider_operation_ref=f"operation:{root_kind}:{entry_path}",
        run_ref=f"run:{root_kind}", attempt_ref=f"attempt:{root_kind}:{entry_path}", attempt_generation=1,
        root_session_ref=f"root:{root_kind}", fence_ref=f"fence:{root_kind}", model_ref=CODEX_MODEL_REF,
        prompt="Continue the current authorized work.", mcp_url="http://127.0.0.1:0/mcp", mcp_token="test-bearer",
        native_session_ref=None if entry_path == "initial" else session,
        root_kind=root_kind, entry_path=entry_path, authorized_operation_ids=operation_ids,
    ))
    assert len(reading.observed) == 1
    developer = reading.observed[0][1]
    assert ("cycle-work.md" in developer) == ("cycle-work.md" in names)
    assert result.profile["root_capability_diagnostics"]["side_effect_authorization"]["operation_ids"] == list(operation_ids)
