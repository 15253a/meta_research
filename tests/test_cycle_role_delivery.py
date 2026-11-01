"""Observe role methods and disclosed files at the native provider boundary.

The controlled provider returns deterministic output; these tests verify actual
delivery and readable installed files, not a model's scientific judgement.
"""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import re
from io import StringIO

import pytest

from meta_research.plan_skill import CodexPlanSkillAdapter
from test_plan_skill_adapter import _fake_codex, _plan, _request, _SequenceRunner
from test_public_guidance_delivery import _ReadingRunner


class _RoleReadingRunner(_ReadingRunner):
    def __init__(self, delegate, role_directory):
        super().__init__(delegate)
        self.role_directory = role_directory
        self.role_observations = []

    def run_job(self, job_ref, argv, prompt, timeout, environment=None):
        # The deterministic delegate has no subprocess lifetime to manage.
        del job_ref
        return self(argv, prompt, timeout, environment)

    def __call__(self, argv, prompt, timeout, environment=None):
        # The shared observer reads through the same package root exposed to the
        # provider. Defer the actual result until the role resources are read.
        from test_public_guidance_delivery import _developer
        developer = _developer(argv)
        root = Path(re.search(r"^Shared reference root: (.+)$", developer, re.M).group(1))
        cwd = Path(argv[argv.index("--cd") + 1])
        role = root / self.role_directory
        skill = role / "SKILL.md"
        assert str(skill) in prompt + developer
        skill_body = skill.read_text(encoding="utf-8")
        documents = {path: path.read_text(encoding="utf-8") for path in role.rglob("*.md")}
        links = []
        for path, body in documents.items():
            assert ".scratch/" not in body
            for target in re.findall(r"\]\(([^)]+)\)", body):
                relative = target.split("#", 1)[0]
                if not relative or "://" in relative:
                    continue
                linked = (path.parent / relative).resolve()
                assert linked.is_relative_to(root) and linked.is_file(), (path, target)
                links.append((str(path.relative_to(root)), target))
            if path != skill:
                assert body.strip() and body not in prompt and body not in developer, path
        assert skill_body not in prompt, "business input repeats Harness-maintained role methods"
        assert skill_body.strip() in developer, "Harness must maintain the role's short methods"
        evidence = {
            "cwd": str(cwd), "role": self.role_directory,
            "provider_argv": argv,
            "system_channel": "provider-owned; no separate application system payload",
            "channels": {"developer": developer, "user": prompt},
            "reference_reads": {str(path.relative_to(root)): {
                "utf8_bytes": len(body.encode()), "sha256": hashlib.sha256(body.encode()).hexdigest(),
            } for path, body in documents.items()},
            "links_checked": links,
        }
        (cwd / f"cycle-role-observation-{len(self.role_observations)}.json").write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
        self.role_observations.append(evidence)
        return super().__call__(argv, prompt, timeout, environment)


def test_plan_delivers_methods_and_discloses_contracts_then_resumes_with_science(tmp_path):
    runner = _RoleReadingRunner(_SequenceRunner([{"plan": _plan()}, {"plan": _plan("Corrected device comparison")}]), "plan_stage")
    adapter = CodexPlanSkillAdapter(tmp_path / "provider", executable=str(_fake_codex(tmp_path / "codex")), process_runner=runner)
    request = _request(runtime_binding=adapter.runtime_binding())
    first = adapter.generate_draft(request)
    revised = replace(request, native_session_ref=first.primary_session_ref,
        submission_revision=2, predecessor_submission_ref="plan-submission:1",
        owner_rejection_receipt_ref="receipt:rejection:1", owner_rejection_kind="domain",
        owner_feedback=("Correct the device comparison, preserve the frozen IdeaSet.",))
    result = adapter.generate_draft(revised)
    assert result.primary_session_ref == first.primary_session_ref
    assert runner.calls[1][0][-3:] == ["resume", first.primary_session_ref, "-"]
    assert runner.calls[0][2] == runner.calls[1][2]
    for _, user, _ in runner.calls:
        assert "哪种约束保留稀有形态？" in user and "比较拓扑一致性与像素重建。" in user
    assert "Correct the device comparison, preserve the frozen IdeaSet." in runner.calls[1][1]
    assert len(runner.role_observations) == 2


def test_idea_continuation_keeps_methods_and_original_session(tmp_path):
    from meta_research.idea_skill import CodexIdeaSkillAdapter
    from test_idea_skill_contract import _request as idea_request, _idea_set, _SequenceRunner as IdeaRunner
    runner = _RoleReadingRunner(IdeaRunner([
        {"outcome": _idea_set()}, {"outcome": _idea_set("沿同一原生会话吸收人工回应后继续")},
    ], thread_ids=["codex-primary:1", "codex-primary:1"]), "idea_stage")
    adapter = CodexIdeaSkillAdapter(tmp_path / "provider", executable=str(_fake_codex(tmp_path / "codex")), process_runner=runner)
    request = idea_request(runtime_binding=adapter.runtime_binding(), job_ref="idea-role-delivery")
    first = adapter.generate_draft(request)
    continued = adapter.generate_draft(replace(request, job_ref="root_hr_continuation_" + "1" * 64,
        native_session_ref=first.primary_session_ref))
    assert continued.primary_session_ref == first.primary_session_ref
    assert runner.calls[1][0][-3:] == ["resume", first.primary_session_ref, "-"]
    assert "accepted_question=" in runner.calls[0][1]
    assert "accepted_question=" in runner.calls[1][1]
    assert runner.calls[0][2] == runner.calls[1][2]


def test_bundle_rolling_dispatch_keeps_methods_and_delivers_current_frontier(tmp_path):
    from meta_research.bundle_skill import CodexBundleSkillAdapter, BundleSkillRequest, BundleDispatchRequest
    from meta_research.owners.common import canonical_hash, canonical_json
    from test_bundle_skill_adapter import _plan_document, _target_plan, _inbox_checkpoint, _FullConformanceAuthority, _SequenceRunner as BundleRunner
    plan = _plan_document()
    position = {"cycle_ref": "cycle:bundle-1", "round": 1, "cycle_focus": "Audit frozen scientific inputs", "focus_source": {"request_ref": "fixture-start"}}
    context = {
        "schema_ref": "meta-research/bundle-context-pack/v1", "cycle_ref": "cycle:bundle-1",
        "cycle_position": position,
        "accepted_question_binding": {"question_ref": "question:bundle-1"},
        "accepted_formal_plan_binding": {"formal_plan_ref": "formal-plan:bundle-1", "plan_document": plan,
            "plan_document_hash": canonical_hash(plan), "answer_contract_hash": "a" * 64},
    }
    context_hash = canonical_hash(context)
    runner = _RoleReadingRunner(BundleRunner([
        {"target_plan": _target_plan(plan, context_hash)},
        {"action": "dispatch", "selected_target_ref": "target:accepted-structure", "rationale": "This frontier item closes the frozen gap."},
    ]), "bundle_stage")
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
    first = adapter.execute(request)
    dispatch = adapter.schedule_target(BundleDispatchRequest(
        cycle_ref=request.cycle_ref, cycle_position=position, stage_request_ref=request.stage_request_ref,
        run_ref=request.run_ref, attempt_ref=request.attempt_ref, fence_ref=request.fence_ref,
        graph_ref="target-graph:1", generation=1,
        frontier=({"target_ref": "target:accepted-structure", "target_key": "target:structure"},),
        state={"schema_ref": "meta-research/bundle-dispatch-state/v1", "target_commit_refs": [], "running_targets": [], "blocked_targets": []},
        root_session_ref=request.root_session_ref, native_session_ref=first.primary_session_ref,
        runtime_binding=request.runtime_binding, inbox_checkpoint=request.inbox_checkpoint,
    ))
    assert canonical_json(plan) in runner.calls[0][1]
    assert dispatch.native_session_ref == first.primary_session_ref
    assert dispatch.selected_target_ref == "target:accepted-structure"
    assert runner.calls[1][0][-3:] == ["resume", first.primary_session_ref, "-"]
    assert "target:accepted-structure" in runner.calls[1][1]
    assert len(authority.issued) == len(authority.revoked) == 2


def test_reasoning_methods_survive_completion_rejection_and_successor_resume(tmp_path):
    from meta_research.reasoning_skill import CodexReasoningSkillAdapter
    from test_reasoning_skill_adapter import _request as reasoning_request, _stage_output, _SequenceRunner as ReasoningRunner, _FullConformanceAuthority, _autonomous_resume_fixture
    base = reasoning_request()
    checkpoint, creation, review = _autonomous_resume_fixture(base)
    runner = _RoleReadingRunner(ReasoningRunner([_stage_output(), review]), "reasoning_stage")
    adapter = CodexReasoningSkillAdapter(tmp_path / "provider", executable=str(_fake_codex(tmp_path / "codex")), process_runner=runner)
    authority = _FullConformanceAuthority()
    adapter.bind_full_conformance_authority(authority)
    adapter.configure_resident_mcp_endpoint("http://127.0.0.1:8765")
    request = replace(base, runtime_binding=adapter.runtime_binding())
    first = adapter.generate_draft(request)
    resumed = adapter.resume_after_autonomous_creation(replace(request,
        native_session_ref=first.primary_session_ref,
        predecessor_candidate_ref="reasoning-candidate:rejected",
        owner_rejection_receipt_ref="receipt:reasoning-completion-rejected", owner_rejection_kind="completion",
        owner_feedback=("修正 autonomous completion 的 transition 表达。",)), checkpoint, creation)
    assert resumed.primary_session_ref == first.primary_session_ref
    assert runner.calls[1][0][-3:] == ["resume", first.primary_session_ref, "-"]
    assert "修正 autonomous completion 的 transition 表达" in runner.calls[1][1]
    assert "question:autonomous:1" in runner.calls[1][1]
    assert len(authority.issued) == len(authority.revoked) == 2


def test_target_admission_delivers_methods_and_exact_owner_science(tmp_path):
    from meta_research.harness_adapters import CodexHarnessAdapter
    from test_harness_adapters import _RecordedRunner
    from test_target_root_finalizer import _current_bundle_runtime
    from test_target_launch_admission import _ready_launch

    class Delivered(BaseException):
        pass

    def stop_after_delivery(argv, prompt, timeout, environment):
        assert "Exact Owner context:\n" in prompt
        raise Delivered()

    reading = _RoleReadingRunner(stop_after_delivery, "target-execution")
    probes = _RecordedRunner("codex", ())

    def runner(argv, prompt, timeout, environment):
        if "--cd" not in argv:
            return probes(argv, prompt, timeout, environment)
        return reading(argv, prompt, timeout, environment)

    adapter = CodexHarnessAdapter(tmp_path / "native-harness", runner=runner)
    runtime = _current_bundle_runtime(tmp_path / "runtime", harness_adapters=(adapter,))
    try:
        graph, target, _bundle, dispatch, launch = _ready_launch(runtime)
        runtime.owners.agent_runtime.admit_target_launch(launch, dispatch_decision_ref=dispatch.decision_ref,
            idempotency_key="cycle-role-target")
        runtime.configure_resident_mcp_endpoint("http://127.0.0.1:8765")
        runtime.target_run_runtime.configure_resident_mcp_endpoint("http://127.0.0.1:8765")
        for _ in range(16):
            try:
                runtime.target_run_runtime.process_once(target.target_ref)
            except Delivered:
                break
        assert len(reading.role_observations) == 1, runtime.target_run_runtime.query_status(target.target_ref)
        user = reading.role_observations[0]["channels"]["user"]
        material, _ = json.JSONDecoder().raw_decode(user.split("Exact Owner context:\n", 1)[1])
        assert material["research_context"]["target_ref"] == target.target_ref
        assert material["research_context"]["graph_ref"] == graph.graph_ref
        assert material["execution_contract"]["measurement_contract"]
        original = runtime.target_run_authorities.research_graph.query_target_research_context(target_ref=target.target_ref)
        for field in ("question", "plan_notes", "bundle_notes"):
            assert material["research_context"][field] == original[field]
        assert Path(material["frozen_input_manifest_path"]).is_file()
    finally:
        runtime.close()


def test_target_resume_harness_maintains_role_and_exact_authorization(tmp_path):
    from meta_research.codex_runtime import CODEX_MODEL_REF
    from meta_research.harness_adapters import CodexHarnessAdapter, HarnessInvocation
    from meta_research.root_resident_mcp import RootResidentMcpChannels
    from test_root_capability_floor import _NoActivityCodexRunner
    session = "session:target"
    reading = _RoleReadingRunner(_NoActivityCodexRunner(session), "target-execution")

    def runner(argv, prompt, timeout, environment):
        if "--cd" not in argv:
            return reading.delegate(argv, prompt, timeout, environment)
        return reading(argv, prompt, timeout, environment)

    operations = RootResidentMcpChannels("target").operation_ids
    result = CodexHarnessAdapter(tmp_path / "target", runner=runner).invoke(HarnessInvocation(
        harness_family="codex", provider_operation_ref="operation:target:resume",
        run_ref="run:target", attempt_ref="attempt:target:resume", attempt_generation=1,
        root_session_ref="root:target", fence_ref="fence:target", model_ref=CODEX_MODEL_REF,
        prompt="Continue the current authorized work.", mcp_url="http://127.0.0.1:0/mcp", mcp_token="test-bearer",
        native_session_ref=session, root_kind="target", entry_path="resume", authorized_operation_ids=operations,
    ))
    assert len(reading.role_observations) == 1
    assert result.profile["root_capability_diagnostics"]["side_effect_authorization"]["operation_ids"] == list(operations)


@pytest.mark.parametrize("reference", ("formal-work.md", "data-preservation.md"))
def test_target_reference_revision_changes_the_bound_capability_profile(monkeypatch, reference):
    from meta_research.root_capabilities import root_capability_profile
    before = root_capability_profile("target").digest
    original_open = Path.open

    def changed(path, *args, **kwargs):
        if path.name == reference and path.parent.parent.name == "target-execution":
            with original_open(path, *args, **kwargs) as stream:
                return StringIO(stream.read() + "\nA revised Target handoff requirement.\n")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", changed)
    assert root_capability_profile("target").digest != before


@pytest.mark.parametrize("role", ("idea", "plan", "bundle", "reasoning"))
@pytest.mark.parametrize("missing_resource", ("formal-work.md", "data-preservation.md", "other-role"))
def test_stage_binding_reports_cross_role_resource_failure_before_provider(tmp_path, monkeypatch, role, missing_resource):
    from importlib import import_module

    module = import_module(f"meta_research.{role}_skill")
    adapter_type = getattr(module, f"Codex{role.title()}SkillAdapter")
    unavailable = getattr(module, f"{role.title()}SkillUnavailable")
    provider_calls = []

    def runner(*args, **kwargs):
        provider_calls.append(args)
        raise AssertionError("missing installed resource must prevent provider calls")

    adapter = adapter_type(tmp_path / "provider", executable=str(_fake_codex(tmp_path / "codex")), process_runner=runner)
    other_role = "plan_stage" if role == "idea" else "idea_stage"
    suffix = (f"/{other_role}/SKILL.md" if missing_resource == "other-role"
              else f"/target-execution/references/{missing_resource}")
    original_read = Path.read_text

    def missing(path, *args, **kwargs):
        if path.as_posix().endswith(suffix):
            raise FileNotFoundError("required cross-role resource unavailable")
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", missing)
    with pytest.raises(unavailable, match=f"^{role}_skill_resource_unavailable$") as caught:
        adapter.runtime_binding()
    assert isinstance(caught.value.__cause__, FileNotFoundError)
    assert provider_calls == []


@pytest.mark.parametrize("role", ("idea", "plan", "bundle", "reasoning"))
def test_stage_binding_reads_each_role_resource_once_per_preparation(tmp_path, monkeypatch, role):
    from collections import Counter
    from importlib import import_module
    from meta_research.research_guidance import CYCLE_ROLE_DIRECTORIES, shared_reference_root

    module = import_module(f"meta_research.{role}_skill")
    adapter_type = getattr(module, f"Codex{role.title()}SkillAdapter")
    adapter = adapter_type(tmp_path / "provider", executable=str(_fake_codex(tmp_path / "codex")))
    root = shared_reference_root()
    reads = Counter()
    original_read = Path.read_text

    def counted(path, *args, **kwargs):
        if path.is_relative_to(root) and path.suffix == ".md":
            reads[path.relative_to(root).as_posix()] += 1
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", counted)
    first = adapter.runtime_binding()
    expected_role_resources = {f"{directory}/SKILL.md" for directory in CYCLE_ROLE_DIRECTORIES.values()}
    expected_role_resources.update(f"target-execution/references/{name}" for name in ("formal-work.md", "data-preservation.md"))
    assert expected_role_resources <= reads.keys()
    assert set(reads.values()) == {1}
    assert adapter.runtime_binding() == first
    assert set(reads.values()) == {2}, "a fresh preparation must read current resource bytes again"


def test_standalone_profile_prepares_one_resource_root_and_observes_next_revision(monkeypatch):
    from meta_research.root_capabilities import root_capability_profile

    original_resolve, original_read = Path.resolve, Path.read_text
    resolutions = []
    revision = ""

    def resolved(path, *args, **kwargs):
        if path.name == "skills" and path.parent.name == "meta_research":
            resolutions.append(str(path))
        return original_resolve(path, *args, **kwargs)

    def read(path, *args, **kwargs):
        body = original_read(path, *args, **kwargs)
        if path.as_posix().endswith("/target-execution/references/formal-work.md"):
            return body + revision
        return body

    monkeypatch.setattr(Path, "resolve", resolved)
    monkeypatch.setattr(Path, "read_text", read)
    first = root_capability_profile("plan").as_dict()
    assert len(resolutions) == 1
    revision = "\nCurrent Target resource revision.\n"
    second = root_capability_profile("plan").as_dict()
    assert len(resolutions) == 2
    assert first["research_system_prompt_hash"] != second["research_system_prompt_hash"]


@pytest.mark.parametrize("resource", ("SKILL.md", "references/formal-work.md", "references/data-preservation.md"))
def test_missing_target_role_resource_fails_before_native_model_delivery(tmp_path, monkeypatch, resource):
    from meta_research.codex_runtime import CODEX_MODEL_REF
    from meta_research.harness_adapters import CodexHarnessAdapter, HarnessAdapterUnavailable, HarnessInvocation
    from meta_research.root_resident_mcp import RootResidentMcpChannels
    from test_root_capability_floor import _NoActivityCodexRunner
    original_open = Path.open

    def missing(path, *args, **kwargs):
        if path.as_posix().endswith("/target-execution/" + resource):
            raise FileNotFoundError("required Target role resource unavailable")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", missing)
    runner = _NoActivityCodexRunner("session:target")
    invocation = HarnessInvocation(
        harness_family="codex", provider_operation_ref="operation:target:initial",
        run_ref="run:target", attempt_ref="attempt:target:initial", attempt_generation=1,
        root_session_ref="root:target", fence_ref="fence:target", model_ref=CODEX_MODEL_REF,
        prompt="Execute the authorized Target.", mcp_url="http://127.0.0.1:0/mcp", mcp_token="test-bearer",
        root_kind="target", entry_path="initial", authorized_operation_ids=RootResidentMcpChannels("target").operation_ids,
    )
    with pytest.raises(HarnessAdapterUnavailable, match="provider_resource_unavailable"):
        CodexHarnessAdapter(tmp_path / "target", runner=runner).invoke(invocation)
    assert all("--cd" not in argv and "features" not in argv for argv in runner.calls)


def test_admitted_target_records_missing_role_resource_as_pending(tmp_path, monkeypatch):
    from meta_research.harness_adapters import CodexHarnessAdapter
    from test_harness_adapters import _RecordedRunner
    from test_target_root_finalizer import _current_bundle_runtime
    from test_target_launch_admission import _ready_launch
    probes = _RecordedRunner("codex", ())
    model_calls = []

    def runner(argv, prompt, timeout, environment):
        if "--cd" in argv:
            model_calls.append(argv)
            raise AssertionError("unavailable role resource must prevent model delivery")
        return probes(argv, prompt, timeout, environment)

    runtime = _current_bundle_runtime(tmp_path / "runtime", harness_adapters=(
        CodexHarnessAdapter(tmp_path / "harness", runner=runner),))
    try:
        _graph, target, _bundle, dispatch, launch = _ready_launch(runtime)
        runtime.owners.agent_runtime.admit_target_launch(launch, dispatch_decision_ref=dispatch.decision_ref,
            idempotency_key="target-missing-role-resource")
        for _ in range(16):
            runtime.target_run_runtime.process_once(target.target_ref)
            status = runtime.target_run_runtime.query_status(target.target_ref)
            if status is not None and status.phase == "root_pending":
                break
        assert status.phase == "root_pending"
        runtime.configure_resident_mcp_endpoint("http://127.0.0.1:8765")
        runtime.target_run_runtime.configure_resident_mcp_endpoint("http://127.0.0.1:8765")
        original_open = Path.open

        def missing(path, *args, **kwargs):
            if path.as_posix().endswith("/target-execution/references/formal-work.md"):
                raise FileNotFoundError("Target reference is unavailable")
            return original_open(path, *args, **kwargs)

        monkeypatch.setattr(Path, "open", missing)
        assert runtime.target_run_runtime.process_once(target.target_ref) is False
        status = runtime.target_run_runtime.query_status(target.target_ref)
        assert status.phase == "root_pending"
        assert status.pending_code == "provider_resource_unavailable"
        assert model_calls == []
    finally:
        runtime.close()
