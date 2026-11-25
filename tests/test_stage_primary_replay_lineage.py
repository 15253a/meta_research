from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from provider_supervisor_support import closed_tree_fixture

from meta_research.bundle_skill import CodexBundleSkillAdapter, BundleSkillUnavailable
from meta_research.composition import build_production_runtime
from meta_research.idea_skill import CodexIdeaSkillAdapter, IdeaSkillUnavailable
from meta_research.owners.common import canonical_hash
from meta_research.paths import prepare_data_root
from meta_research.reasoning_skill import CodexReasoningSkillAdapter, ReasoningSkillUnavailable
import test_bundle_input_drift_recovery as bundle_drift_fixtures
from test_bundle_exhaustion_owner import _ExhaustionBundleSkill
from test_bundle_skill_adapter import (
    _SequenceRunner as _BundleSequenceRunner,
    _fake_codex,
    _inbox_checkpoint,
    _provider_wire_value,
)
from test_harness_full_conformance import _FullConformanceAdapter, _full_request
from test_public_bundle_stage import _bundle_runtime, _prepare_bundle_request
from test_public_plan_stage import (
    _DeterministicDraftingAdapter,
    _DeterministicIdeaSkill,
    _DeterministicPlanSkill,
    _DeterministicProbe,
    _confirm_direct_quest,
    _finish_idea_stage,
)
from test_public_reasoning_stage import (
    _DeterministicReasoningSkill,
    _confirm_deepfetch_quest,
    _reasoning_runtime,
)
from test_reasoning_skill_adapter import _SequenceRunner as _ReasoningSequenceRunner


class _SimulatedCrashAfterPrimarySpool(RuntimeError):
    """Simulates daemon death after a sealed primary provider operation finished."""


class _CrashOnceIdeaAdapter(CodexIdeaSkillAdapter):
    def __init__(self, *args, crash_on_first: bool, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._crash_on_first = crash_on_first

    def generate_draft(self, request):
        draft = super().generate_draft(request)
        if self._crash_on_first:
            self._crash_on_first = False
            raise _SimulatedCrashAfterPrimarySpool("simulated sigkill after spool write")
        return draft


class _CrashOnceReasoningAdapter(CodexReasoningSkillAdapter):
    def __init__(self, *args, crash_on_first: bool, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._crash_on_first = crash_on_first

    def generate_draft(self, request):
        draft = super().generate_draft(request)
        if self._crash_on_first:
            self._crash_on_first = False
            raise _SimulatedCrashAfterPrimarySpool("simulated sigkill after spool write")
        return draft


class _CrashOnceBundleAdapter(CodexBundleSkillAdapter):
    def __init__(self, *args, crash_on_first: bool, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._crash_on_first = crash_on_first

    def generate_draft(self, request):
        draft = super().generate_draft(request)
        if self._crash_on_first:
            self._crash_on_first = False
            raise _SimulatedCrashAfterPrimarySpool("simulated sigkill after spool write")
        return draft


class _IdeaSequenceRunner:
    def __init__(self, output_factory) -> None:
        self._output_factory = output_factory
        self.calls: list[tuple[list[str], str, dict[str, object]]] = []

    def __call__(self, argv, prompt, timeout, environment=None):
        import json
        import subprocess

        del timeout, environment
        schema_path = Path(argv[argv.index("--output-schema") + 1])
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        output_path = Path(argv[argv.index("--output-last-message") + 1])
        if self.calls:
            raise AssertionError("duplicate provider invocation should not occur on replay")
        output = self._output_factory(prompt, schema)
        output_path.write_text(json.dumps(output, ensure_ascii=False), encoding="utf-8")
        self.calls.append((argv, prompt, schema))
        stdout = json.dumps({"type": "thread.started", "thread_id": "codex-idea-primary:1"}) + "\n"
        completed = subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")
        completed.process_tree = closed_tree_fixture()
        return completed

    def run_job(self, job_ref, argv, prompt, timeout, environment=None):
        del job_ref
        return self(argv, prompt, timeout, environment)


def _reconcile_revocation_pending_units(runtime, run_ref: str) -> None:
    """Acknowledge revoked Attempt 1 units just as daemon 2 cleanup does once exited."""
    from sqlalchemy import text

    with runtime._database.read() as connection:
        rows = connection.execute(
            text(
                "SELECT unit_ref, run_ref, attempt_ref, fence_ref FROM ar_provider_units "
                "WHERE run_ref = :run_ref AND status = 'revocation_pending'"
            ),
            {"run_ref": run_ref},
        ).all()
    for row in rows:
        runtime.owners.agent_runtime.acknowledge_provider_safe_point(
            unit_ref=str(row.unit_ref),
            run_ref=str(row.run_ref),
            attempt_ref=str(row.attempt_ref),
            fence_ref=str(row.fence_ref),
        )


def test_idea_stage_crash_recovery_replays_sealed_spool_and_commits(tmp_path: Path) -> None:
    data_root_path = tmp_path / "idea-data"
    provider_dir = tmp_path / "idea-provider"
    codex_exe = str(_fake_codex(tmp_path / "codex"))
    captured: dict[str, str] = {}

    def make_output(_prompt, _schema):
        return {
            "outcome": {
                "kind": "NoViableCandidate",
                "payload": {
                    "question_ref": captured["question_ref"],
                    "context_pack_ref": captured["context_pack_ref"],
                    "exploration_scope": "比较当前证据支持的结构保持机制。",
                    "candidate_families_considered": [
                        {
                            "family": "跨增强结构一致性",
                            "why_not_viable": "当前证据没有可识别稀有形态的代理信号。",
                            "evidence_refs": [],
                        }
                    ],
                    "evidence_boundary": {
                        "accepted_evidence_refs": [],
                        "supported": "Accepted Question 仅固定了低照度场景。",
                        "inferred": "现有代理目标不足以支持负责的候选。",
                        "unknown": "补充形态标注后能否形成候选仍未知。",
                    },
                    "overturn_conditions": ["接纳包含稀有形态标注的新 Evidence。"],
                    "why_plan_cannot_proceed": "当前没有可冻结为实验承诺的机制。",
                },
            }
        }

    runner = _IdeaSequenceRunner(make_output)

    def open_runtime(*, crash_on_first: bool, port: int):
        drafting = _DeterministicDraftingAdapter()
        adapter = _CrashOnceIdeaAdapter(
            provider_dir,
            executable=codex_exe,
            process_runner=runner,
            crash_on_first=crash_on_first,
        )
        rt = build_production_runtime(
            prepare_data_root(data_root_path),
            proposal_drafter=drafting,
            intent_drafting_provider=drafting,
            host_compute_probe=_DeterministicProbe(),
            idea_skill_provider=adapter,
            harness_adapters=(_FullConformanceAdapter("codex"),),
        )
        if rt.harnesses.query_status()["status"] != "ready":
            rt.harnesses.start_full_conformance(_full_request())
            for _turn in range(4):
                if rt.harnesses.query_status()["status"] == "ready":
                    break
                assert rt.harnesses.advance_full_conformance(
                    mcp_base_url=f"http://127.0.0.1:{port}"
                )
        rt.configure_resident_mcp_endpoint(f"http://127.0.0.1:{port}")
        return rt, adapter

    runtime, adapter1 = open_runtime(crash_on_first=True, port=8761)
    try:
        completed = _confirm_direct_quest(runtime)
        assert runtime.idea_stage.process_once()  # AE request
        assert runtime.idea_stage.process_once()  # AR admission
        current = runtime.idea_stage.query_current()
        req = current["stage_run_request"]
        stage_req = runtime.owners.advancement_engine.query_idea_stage_request(req["cycle_ref"])
        assert stage_req is not None
        captured["question_ref"] = stage_req.accepted_question.question_ref
        captured["context_pack_ref"] = stage_req.context_pack_ref
        run_before = runtime.owners.agent_runtime.query_idea_stage_run(req["request_ref"])
        assert run_before is not None

        try:
            runtime.idea_stage.process_once()
        except _SimulatedCrashAfterPrimarySpool:
            pass
        else:
            raise AssertionError(
                f"Expected simulated crash after primary spool, got transient_error={runtime.idea_stage._transient_error!r}"
            )
        # Simulate abrupt death without safe-point acknowledgement by leaving unit active
        from sqlalchemy import text
        with runtime._database.write() as connection:
            connection.execute(
                text(
                    "UPDATE ar_provider_units SET status = 'active', completed_at = NULL "
                    "WHERE unit_ref = :unit_ref"
                ),
                {"unit_ref": run_before.primary_invocation.invocation_ref},
            )
        assert len(runner.calls) == 1
    finally:
        runtime.close()

    restarted, adapter2 = open_runtime(crash_on_first=False, port=8791)
    try:
        run_after = restarted.owners.agent_runtime.query_idea_stage_run(req["request_ref"])
        assert run_after is not None
        assert run_after.attempt_ref != run_before.attempt_ref
        assert run_after.fence_ref != run_before.fence_ref
        assert run_after.attempt_generation == 2
        assert run_after.primary_invocation.operation_ref == run_before.primary_invocation.operation_ref
        assert run_after.replayable_primary_attempts == (
            (run_before.attempt_ref, run_before.fence_ref, 1),
        )
        _reconcile_revocation_pending_units(restarted, run_after.run_ref)

        for _ in range(12):
            state = restarted.idea_stage.query_current()
            if state["stage_commit"] is not None:
                break
            assert restarted.idea_stage.process_once()

        final_state = restarted.idea_stage.query_current()
        assert final_state["stage_commit"] is not None
        assert final_state["stage_commit"]["stage"] == "Idea"
        assert final_state["outcome_acceptance"]["status"] == "accepted"
        assert len(runner.calls) == 1

        # Negative security check: foreign lineage must still fail closed.
        forged_request = replace(
            _idea_request_from_run(restarted, run_after, req),
            replayable_primary_attempts=(("foreign-attempt", run_before.fence_ref, 1),),
        )
        with pytest.raises(IdeaSkillUnavailable, match="^codex_operation_identity_conflict$"):
            adapter2.generate_draft(forged_request)
    finally:
        restarted.close()


def _idea_request_from_run(runtime, run, req):
    from meta_research.idea_skill import IdeaSkillRequest

    stage_req = runtime.owners.advancement_engine.query_idea_stage_request(req["cycle_ref"])
    assert stage_req is not None
    accepted_content = runtime.owners.research_memory.read_question_content(
        stage_req.accepted_question.content_ref, stage_req.accepted_question.content_hash
    )
    return IdeaSkillRequest(
        stage_request_ref=stage_req.request_ref,
        question_ref=stage_req.accepted_question.question_ref,
        context_pack_ref=stage_req.context_pack_ref,
        context_pack_hash=stage_req.context_pack_hash,
        context_pack=stage_req.context_pack,
        accepted_question_content=accepted_content,
        root_session_ref=run.root_session_ref,
        submission_revision=run.attempt_generation,
        runtime_binding=run.runtime_binding,
        native_session_ref=run.native_session_ref,
        job_ref=run.primary_invocation.operation_ref,
        run_ref=run.run_ref,
        attempt_ref=run.attempt_ref,
        fence_ref=run.fence_ref,
        replayable_primary_attempts=run.replayable_primary_attempts,
    )


def test_reasoning_stage_crash_recovery_replays_sealed_spool_and_commits(tmp_path: Path) -> None:
    data_root_path = tmp_path / "reasoning-data"
    provider_dir = tmp_path / "reasoning-provider"
    codex_exe = str(_fake_codex(tmp_path / "codex"))
    helper_skill = _DeterministicReasoningSkill()
    output_box: list[dict[str, object]] = []

    class _DynamicReasoningRunner(_ReasoningSequenceRunner):
        def __call__(self, argv, prompt, timeout, environment=None):
            import json
            import subprocess

            del timeout
            if self.calls:
                raise AssertionError("duplicate provider invocation should not occur on replay")
            schema_path = Path(argv[argv.index("--output-schema") + 1])
            schema = json.loads(schema_path.read_text(encoding="utf-8"))
            output_path = Path(argv[argv.index("--output-last-message") + 1])
            output = next(self._outputs)
            if (
                set(schema.get("properties", {})) == {"provider_output"}
                and set(output) != {"provider_output"}
            ):
                output = {"provider_output": output}
            output_path.write_text(json.dumps(output, ensure_ascii=False), encoding="utf-8")
            self.calls.append((argv, prompt, schema))
            self.environments.append(environment)
            stdout = json.dumps({"type": "thread.started", "thread_id": "provider-session:1"}) + "\n"
            completed = subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")
            completed.process_tree = closed_tree_fixture()
            return completed

    runner = _DynamicReasoningRunner(output_box, emit_review_trace=False)

    def open_runtime(*, crash_on_first: bool, port: int):
        adapter = _CrashOnceReasoningAdapter(
            provider_dir,
            executable=codex_exe,
            process_runner=runner,
            crash_on_first=crash_on_first,
        )
        rt = _reasoning_runtime(data_root_path, reasoning_skill=adapter)
        rt.configure_resident_mcp_endpoint(f"http://127.0.0.1:{port}")
        return rt, adapter

    runtime, _adapter1 = open_runtime(crash_on_first=True, port=8765)
    try:
        _confirm_deepfetch_quest(runtime)
        _finish_idea_stage(runtime)
        assert runtime.reasoning_stage.process_once()  # AE request
        assert runtime.reasoning_stage.process_once()  # AR admission
        current = runtime.reasoning_stage.query_current()
        req_ref = current["stage_run_request"]["request_ref"]
        run_before = runtime.owners.agent_runtime.query_reasoning_stage_run(req_ref)
        assert run_before is not None
        stage_req = runtime.owners.advancement_engine.query_reasoning_stage_request(
            current["stage_run_request"]["cycle_ref"]
        )
        assert stage_req is not None
        skill_req = runtime.reasoning_stage._skill_request(
            stage_req, run_before, job_ref=run_before.primary_invocation.operation_ref
        )
        draft_doc = helper_skill.generate_draft(skill_req).draft
        draft_doc["scientific_outcome"]["notes"] = "Bounded literature support; no new experiment was performed."
        time_state = runtime.owners.advancement_engine.query_research_time(skill_req.quest_ref)
        draft_doc["next_cycle_proposal"]["research_time_basis"] = {
            "schema_ref": "meta-research/research-time-basis/v1",
            "quest_ref": skill_req.quest_ref,
            "budget_revision": skill_req.research_time_budget_revision or time_state["budget_revision"],
            "observed_at": time_state["observed_at"],
            "status": time_state["status"],
            "accuracy": time_state["accuracy"],
            "used_seconds": time_state["used_seconds"],
            "remaining_seconds": time_state["remaining_seconds"],
            "overrun_seconds": time_state["overrun_seconds"],
            "unknown_intervals": list(time_state["unknown_intervals"]),
            "decision": "continue",
            "decision_rationale": "The remaining budget supports the next bounded comparison.",
        }
        output_box.append(draft_doc)
        runner._outputs = iter(output_box)

        try:
            runtime.reasoning_stage.process_once()
        except _SimulatedCrashAfterPrimarySpool:
            pass
        else:
            raise AssertionError(
                f"Expected simulated crash after primary spool, got transient_error={runtime.reasoning_stage._transient_error!r}"
            )
        from sqlalchemy import text
        with runtime._database.write() as connection:
            connection.execute(
                text(
                    "UPDATE ar_provider_units SET status = 'active', completed_at = NULL "
                    "WHERE unit_ref = :unit_ref"
                ),
                {"unit_ref": run_before.primary_invocation.invocation_ref},
            )
        assert len(runner.calls) == 1
    finally:
        runtime.close()

    restarted, adapter2 = open_runtime(crash_on_first=False, port=8799)
    try:
        run_after = restarted.owners.agent_runtime.query_reasoning_stage_run(req_ref)
        assert run_after is not None
        assert run_after.attempt_ref != run_before.attempt_ref
        assert run_after.fence_ref != run_before.fence_ref
        assert run_after.attempt_generation == 2
        assert run_after.primary_invocation.operation_ref == run_before.primary_invocation.operation_ref
        assert run_after.replayable_primary_attempts == (
            (run_before.attempt_ref, run_before.fence_ref, 1),
        )
        _reconcile_revocation_pending_units(restarted, run_after.run_ref)

        for _ in range(16):
            state = restarted.reasoning_stage.query_current()
            if state["stage_commit"] is not None:
                break
            assert restarted.reasoning_stage.process_once()

        final_state = restarted.reasoning_stage.query_current()
        assert final_state["stage_commit"] is not None
        assert final_state["stage_commit"]["stage"] == "Reasoning"
        assert len(runner.calls) == 1

        # Negative check: foreign attempt cannot consume Attempt 1's spool.
        stage_req2 = restarted.owners.advancement_engine.query_reasoning_stage_request(
            final_state["stage_run_request"]["cycle_ref"]
        )
        assert stage_req2 is not None
        forged = replace(
            restarted.reasoning_stage._skill_request(
                stage_req2, run_after, job_ref=run_after.primary_invocation.operation_ref
            ),
            replayable_primary_attempts=(("foreign-attempt", run_before.fence_ref, 1),),
        )
        with pytest.raises(ReasoningSkillUnavailable, match="^codex_operation_identity_conflict$"):
            adapter2.generate_draft(forged)
    finally:
        restarted.close()


def test_bundle_stage_crash_recovery_replays_sealed_spool_with_new_inbox_checkpoint_and_commits(
    tmp_path: Path,
) -> None:
    data_root_path = tmp_path / "bundle-data"
    provider_dir = tmp_path / "bundle-provider"
    codex_exe = str(_fake_codex(tmp_path / "codex"))
    helper_skill = _ExhaustionBundleSkill()
    output_box: list[dict[str, object]] = []

    class _StrictOnceBundleRunner(_BundleSequenceRunner):
        def __call__(self, argv, prompt, timeout, environment=None):
            if self.calls:
                raise AssertionError("duplicate provider invocation should not occur on replay")
            return super().__call__(argv, prompt, timeout, environment=environment)

    runner = _StrictOnceBundleRunner(output_box, emit_review_trace=False)

    def open_runtime(*, crash_on_first: bool, port: int):
        adapter = _CrashOnceBundleAdapter(
            provider_dir,
            executable=codex_exe,
            process_runner=runner,
            crash_on_first=crash_on_first,
        )
        rt = _bundle_runtime(data_root_path, bundle_skill_provider=adapter)
        rt.configure_resident_mcp_endpoint(f"http://127.0.0.1:{port}")
        return rt, adapter

    runtime, _adapter1 = open_runtime(crash_on_first=True, port=8767)
    try:
        _prepare_bundle_request(runtime)
        assert runtime.bundle_stage.process_once()  # AR admission
        current = runtime.bundle_stage.query_current()
        req_ref = current["stage_run_request"]["request_ref"]
        run_before = runtime.owners.agent_runtime.query_bundle_stage_run(req_ref)
        assert run_before is not None

        stage_req = runtime.owners.advancement_engine.query_bundle_stage_request(
            current["stage_run_request"]["cycle_ref"]
        )
        assert stage_req is not None
        accepted_q = runtime.owners.research_memory.read_question_content(
            stage_req.accepted_question.content_ref, stage_req.accepted_question.content_hash
        )
        from meta_research.bundle_skill import BundleSkillRequest
        dummy_req = BundleSkillRequest(
            stage_request_ref=stage_req.request_ref,
            run_ref=run_before.run_ref,
            attempt_ref=run_before.attempt_ref,
            fence_ref=run_before.fence_ref,
            cycle_ref=stage_req.cycle_ref,
            question_ref=stage_req.accepted_question.question_ref,
            formal_plan_ref=stage_req.accepted_formal_plan.formal_plan_ref,
            context_pack_ref=stage_req.context_pack_ref,
            context_pack_hash=stage_req.context_pack_hash,
            context_pack=stage_req.context_pack,
            accepted_question_content=accepted_q,
            plan_document=stage_req.accepted_formal_plan.plan_document,
            root_session_ref=run_before.root_session_ref,
            runtime_binding=run_before.runtime_binding,
            inbox_checkpoint=_inbox_checkpoint(
                run_ref=run_before.run_ref,
                attempt_ref=run_before.attempt_ref,
                fence_ref=run_before.fence_ref,
            ),
        )
        exhaustion_draft = helper_skill.generate_draft(dummy_req).draft
        output_box.append(exhaustion_draft)
        runner._outputs = iter(output_box)

        with pytest.raises(_SimulatedCrashAfterPrimarySpool):
            runtime.bundle_stage.process_once()
        from sqlalchemy import text
        with runtime._database.write() as connection:
            connection.execute(
                text(
                    "UPDATE ar_provider_units SET status = 'active', completed_at = NULL "
                    "WHERE unit_ref = :unit_ref"
                ),
                {"unit_ref": run_before.primary_invocation.invocation_ref},
            )
        assert len(runner.calls) == 1
    finally:
        runtime.close()

    restarted, _adapter2 = open_runtime(crash_on_first=False, port=8798)
    try:
        run_after = restarted.owners.agent_runtime.query_bundle_stage_run(req_ref)
        assert run_after is not None
        assert run_after.attempt_ref != run_before.attempt_ref
        assert run_after.fence_ref != run_before.fence_ref
        assert run_after.attempt_generation == 2
        assert run_after.primary_invocation.operation_ref == run_before.primary_invocation.operation_ref
        assert run_after.replayable_primary_attempts == (
            (run_before.attempt_ref, run_before.fence_ref, 1),
        )
        _reconcile_revocation_pending_units(restarted, run_after.run_ref)

        for _ in range(16):
            state = restarted.bundle_stage.query_current()
            if state["stage_commit"] is not None:
                break
            assert restarted.bundle_stage.process_once(), restarted.bundle_stage.observation

        final_state = restarted.bundle_stage.query_current()
        assert final_state["stage_commit"] is not None
        assert final_state["stage_commit"]["stage"] == "Bundle"
        assert final_state["stage_commit"]["disposition"] == "exhausted"
        assert len(runner.calls) == 1
    finally:
        restarted.close()


@pytest.mark.parametrize("kind", ("dispatch", "batch"))
def test_bundle_rolling_recovery_replays_replaced_fence_and_detects_input_drift(
    tmp_path: Path, kind: str
) -> None:
    work_dir = tmp_path / kind
    work_dir.mkdir(parents=True, exist_ok=True)
    adapter, runner = bundle_drift_fixtures._adapter(work_dir, bundle_drift_fixtures._output(kind))
    req1 = bundle_drift_fixtures._request(adapter, kind)
    invoke = adapter.schedule_targets if kind == "dispatch" else adapter.propose_target_batch
    original = invoke(req1)
    assert len(runner.calls) == 1

    # Simulate restart on a different ephemeral MCP port + replaced fence + new inbox checkpoint.
    from test_bundle_skill_adapter import _FullConformanceAuthority

    restarted_adapter = CodexBundleSkillAdapter(
        work_dir / "provider",
        executable=str(work_dir / "codex"),
        process_runner=runner,
    )
    restarted_adapter.bind_full_conformance_authority(_FullConformanceAuthority())
    restarted_adapter.configure_resident_mcp_endpoint("http://127.0.0.1:9999")
    restarted_invoke = (
        restarted_adapter.schedule_targets
        if kind == "dispatch"
        else restarted_adapter.propose_target_batch
    )
    cp2 = _inbox_checkpoint(
        run_ref=req1.run_ref, attempt_ref="bundle-attempt:replaced-2", fence_ref="bundle-fence:replaced-2"
    )
    cp2 = {**cp2, "checkpoint_ref": "checkpoint:replaced-2", "checkpoint_revision": 2}
    payload2 = {k: v for k, v in cp2.items() if k not in {"checkpoint_hash", "receipt"}}
    cp2["checkpoint_hash"] = canonical_hash(payload2)
    cp2["receipt"] = {**cp2["receipt"], "subject_ref": "checkpoint:replaced-2"}

    req2 = replace(
        req1,
        attempt_ref="bundle-attempt:replaced-2",
        fence_ref="bundle-fence:replaced-2",
        inbox_checkpoint=cp2,
        replayable_continuation_attempts=((req1.attempt_ref, req1.fence_ref, 1),),
        replayable_inbox_checkpoints=(req1.inbox_checkpoint,),
    )
    replayed = restarted_invoke(req2)
    assert replayed == original
    assert len(runner.calls) == 1

    # Foreign lineage must fail closed.
    forged = replace(req2, replayable_continuation_attempts=(("foreign-attempt", req1.fence_ref, 1),))
    with pytest.raises(BundleSkillUnavailable, match="^codex_operation_identity_conflict$"):
        restarted_invoke(forged)

    # Semantically changed inputs across fence replacement must request controlled correction, not loop.
    drifted = bundle_drift_fixtures._changed(req2, kind)
    with pytest.raises(BundleSkillUnavailable) as drift_err:
        restarted_invoke(drifted)
    assert drift_err.value.code == "bundle_review_result_contract_invalid"
    assert drift_err.value.recovery_checkpoint["contract_failure_detail_code"] == "bundle_operation_inputs_changed"
    assert len(runner.calls) == 1


class _SimulatedCrashAfterRollingSpool(RuntimeError):
    """Simulates daemon death right after a rolling dispatch/batch spool finishes."""


class _QueuedBundleRunner(_BundleSequenceRunner):
    def __init__(self) -> None:
        self._queue: list[dict[str, object]] = []
        super().__init__([], emit_review_trace=True)

    def push(self, output: dict[str, object]) -> None:
        self._queue.append(output)

    def __call__(self, argv, prompt, timeout, environment=None):
        if not self._queue:
            raise AssertionError("unexpected duplicate provider invocation during replay")
        self._outputs = iter([self._queue.pop(0)])
        return super().__call__(argv, prompt, timeout, environment)


class _CrashOnRollingBundleAdapter(CodexBundleSkillAdapter):
    def __init__(
        self,
        *args,
        runner: _QueuedBundleRunner,
        allow_primary: bool,
        allow_dispatch: bool,
        allow_batch: bool,
        crash_on: str | None,
        **kwargs,
    ) -> None:
        super().__init__(*args, process_runner=runner, **kwargs)
        self._queued_runner = runner
        self._allow_primary = allow_primary
        self._allow_dispatch = allow_dispatch
        self._allow_batch = allow_batch
        self._crash_on = crash_on

    def generate_draft(self, request):
        from test_public_bundle_stage import _DeterministicBundleSkill

        if self._allow_primary:
            self._allow_primary = False
            self._queued_runner.push(
                {"target_plan": _DeterministicBundleSkill().generate_draft(request).draft}
            )
        return super().generate_draft(request)

    def schedule_targets(self, request):
        if self._allow_dispatch:
            self._allow_dispatch = False
            selected = () if not request.frontier else (str(request.frontier[-1]["target_ref"]),)
            self._queued_runner.push(
                {
                    "action": "dispatch" if selected else "wait",
                    "selected_target_refs": list(selected),
                    "rationale": "Select the highest-priority durable frontier item.",
                }
            )
        result = super().schedule_targets(request)
        if self._crash_on == "dispatch":
            self._crash_on = None
            raise _SimulatedCrashAfterRollingSpool("simulated sigkill after dispatch spool write")
        return result

    def propose_target_batch(self, request):
        from meta_research.bundle_target_contract import FORMAL_STRATEGY_UPDATE_SCHEMA_REF

        if self._allow_batch:
            self._allow_batch = False
            self._queued_runner.push(
                {
                    "strategy_update": {
                        "schema_ref": FORMAL_STRATEGY_UPDATE_SCHEMA_REF,
                        "revision": request.base_generation + 2,
                        "candidates": [],
                        "requires_accepted_labels": [],
                        "strategy_complete": True,
                    },
                    "rationale": "The frozen FormalPlan is fully covered by committed Targets.",
                }
            )
        result = super().propose_target_batch(request)
        if self._crash_on == "batch":
            self._crash_on = None
            raise _SimulatedCrashAfterRollingSpool("simulated sigkill after target-batch spool write")
        return result


def test_bundle_rolling_dispatch_crash_replays_via_real_owner_and_commits_dispatch(
    tmp_path: Path,
) -> None:
    from sqlalchemy import text
    from meta_research.bundle_stage import _rolling_provider_unit_ref

    data_root_path = tmp_path / "bundle-rolling-dispatch-data"
    provider_dir = tmp_path / "bundle-rolling-dispatch-provider"
    codex_exe = str(_fake_codex(tmp_path / "codex"))
    runner = _QueuedBundleRunner()

    def open_runtime(*, allow_primary: bool, allow_dispatch: bool, crash_on: str | None, port: int):
        adapter = _CrashOnRollingBundleAdapter(
            provider_dir,
            executable=codex_exe,
            runner=runner,
            allow_primary=allow_primary,
            allow_dispatch=allow_dispatch,
            allow_batch=False,
            crash_on=crash_on,
        )
        rt = _bundle_runtime(data_root_path, bundle_skill_provider=adapter)
        rt.configure_resident_mcp_endpoint(f"http://127.0.0.1:{port}")
        return rt, adapter

    runtime, _adapter1 = open_runtime(
        allow_primary=True, allow_dispatch=True, crash_on="dispatch", port=8767,
    )
    try:
        _prepare_bundle_request(runtime)
        current = runtime.bundle_stage.query_current()
        req_ref = current["stage_run_request"]["request_ref"]
        cycle_ref = current["stage_run_request"]["cycle_ref"]

        # Drive through AR admission, primary draft, and TargetGraph admission.
        for _ in range(8):
            graph = runtime.owners.research_graph.query_target_graph(req_ref)
            if graph is not None:
                break
            assert runtime.bundle_stage.process_once()
        graph = runtime.owners.research_graph.query_target_graph(req_ref)
        assert graph is not None
        assert len(runner.calls) == 1

        run_before = runtime.owners.agent_runtime.query_bundle_stage_run(req_ref)
        assert run_before is not None
        assert run_before.primary_draft is not None
        assert run_before.attempt_generation == 1

        # Drive rolling dispatch until the simulated SIGKILL after sealed spool write.
        with pytest.raises(_SimulatedCrashAfterRollingSpool):
            for _ in range(8):
                assert runtime.bundle_stage.process_once()
        assert len(runner.calls) == 2

        dispatch_unit_ref = _rolling_provider_unit_ref(
            operation_ref=run_before.primary_invocation.operation_ref,
            operation_name="dispatch-1",
            attempt_ref=run_before.attempt_ref,
        )
        with runtime._database.write() as connection:
            updated = connection.execute(
                text(
                    "UPDATE ar_provider_units SET status = 'active', completed_at = NULL "
                    "WHERE unit_ref = :unit_ref"
                ),
                {"unit_ref": dispatch_unit_ref},
            )
            assert updated.rowcount == 1
        assert runtime.owners.agent_runtime.query_bundle_dispatch_decisions(run_before.run_ref) == ()
    finally:
        runtime.close()

    # Restart on a different resident MCP port with NO queued provider outputs.
    restarted, _adapter2 = open_runtime(
        allow_primary=False, allow_dispatch=False, crash_on=None, port=8798,
    )
    try:
        run_after = restarted.owners.agent_runtime.query_bundle_stage_run(req_ref)
        assert run_after is not None
        assert run_after.attempt_ref != run_before.attempt_ref
        assert run_after.fence_ref != run_before.fence_ref
        assert run_after.attempt_generation == 2
        assert run_after.primary_invocation.operation_ref == run_before.primary_invocation.operation_ref
        assert run_after.replayable_primary_attempts == ()
        assert run_after.replayable_continuation_attempts == (
            (run_before.attempt_ref, run_before.fence_ref, 1),
        )
        retired_checkpoints = restarted.owners.agent_runtime.query_bundle_inbox_checkpoints_for_attempts(
            run_ref=run_after.run_ref,
            attempts=run_after.replayable_continuation_attempts,
        )
        assert retired_checkpoints
        assert all(
            cp["run_ref"] == run_after.run_ref
            and cp["attempt_ref"] == run_before.attempt_ref
            and cp["fence_ref"] == run_before.fence_ref
            for cp in retired_checkpoints
        )
        _reconcile_revocation_pending_units(restarted, run_after.run_ref)

        for _ in range(8):
            decisions = restarted.owners.agent_runtime.query_bundle_dispatch_decisions(run_after.run_ref)
            if decisions:
                break
            assert restarted.bundle_stage.process_once(), restarted.bundle_stage.observation

        decisions = restarted.owners.agent_runtime.query_bundle_dispatch_decisions(run_after.run_ref)
        assert len(decisions) == 1
        assert decisions[0].action == "dispatch"
        assert decisions[0].attempt_ref == run_after.attempt_ref
        assert decisions[0].fence_ref == run_after.fence_ref
        assert len(runner.calls) == 2
    finally:
        restarted.close()


def test_bundle_rolling_target_batch_crash_replays_via_real_owner_and_commits_bundle(
    tmp_path: Path,
) -> None:
    from sqlalchemy import text
    from meta_research.bundle_stage import _rolling_provider_unit_ref
    from test_public_bundle_stage import _finalize_target_root
    from test_target_root_finalizer import _admit_independent_target_root

    data_root_path = tmp_path / "bundle-rolling-batch-data"
    provider_dir = tmp_path / "bundle-rolling-batch-provider"
    codex_exe = str(_fake_codex(tmp_path / "codex"))
    runner = _QueuedBundleRunner()

    def open_runtime(
        *, allow_primary: bool, allow_dispatch: bool, allow_batch: bool, crash_on: str | None, port: int
    ):
        adapter = _CrashOnRollingBundleAdapter(
            provider_dir,
            executable=codex_exe,
            runner=runner,
            allow_primary=allow_primary,
            allow_dispatch=allow_dispatch,
            allow_batch=allow_batch,
            crash_on=crash_on,
        )
        rt = _bundle_runtime(data_root_path, bundle_skill_provider=adapter)
        rt.configure_resident_mcp_endpoint(f"http://127.0.0.1:{port}")
        return rt, adapter

    runtime, _adapter1 = open_runtime(
        allow_primary=True, allow_dispatch=True, allow_batch=True, crash_on="batch", port=8767,
    )
    try:
        _prepare_bundle_request(runtime)
        current = runtime.bundle_stage.query_current()
        req_ref = current["stage_run_request"]["request_ref"]
        cycle_ref = current["stage_run_request"]["cycle_ref"]

        # Drive through primary draft + dispatch decision + launch request.
        for _ in range(12):
            run_now = runtime.owners.agent_runtime.query_bundle_stage_run(req_ref)
            if run_now is not None:
                decisions = runtime.owners.agent_runtime.query_bundle_dispatch_decisions(run_now.run_ref)
                if decisions:
                    break
            assert runtime.bundle_stage.process_once()

        run_before = runtime.owners.agent_runtime.query_bundle_stage_run(req_ref)
        assert run_before is not None
        decisions = runtime.owners.agent_runtime.query_bundle_dispatch_decisions(run_before.run_ref)
        assert len(decisions) == 1
        assert len(runner.calls) == 2

        graph = runtime.owners.research_graph.query_target_graph(req_ref)
        assert graph is not None and len(graph.targets) == 1
        target = graph.targets[0]
        launch_request = runtime.owners.research_graph.query_target_launch_request(target.target_ref)
        if launch_request is None:
            # Let BundleStageWorker prepare launch inputs / launch request.
            for _ in range(4):
                runtime.bundle_stage.process_once()
                launch_request = runtime.owners.research_graph.query_target_launch_request(target.target_ref)
                if launch_request is not None:
                    break
        assert launch_request is not None

        # Commit the single Target through the real TargetRootLifecycle + TargetRunFinalizer.
        target, candidate, formal_plan, admission, handle = _admit_independent_target_root(
            runtime,
            ready=(graph, target, run_before, decisions[0], launch_request),
            key_prefix="rolling-batch-replay-",
        )
        launch = runtime.owners.agent_runtime.query_admitted_target_launch(target.target_ref)
        assert launch is not None
        runtime.target_root_lifecycle.activate(
            launch_ref=launch.launch_ref,
            handle=handle,
            candidate=candidate,
            formal_plan=formal_plan,
            idempotency_key=f"activate-rolling-batch-replay:{target.target_ref}",
        )
        _finalize_target_root(runtime, launch, handle, admission.run.attempt_generation)
        graph_before_batch = runtime.owners.research_graph.query_target_graph(req_ref)
        assert graph_before_batch is not None and not graph_before_batch.strategy_complete

        # Drive rolling target-batch until simulated SIGKILL after sealed spool write.
        with pytest.raises(_SimulatedCrashAfterRollingSpool):
            for _ in range(8):
                assert runtime.bundle_stage.process_once()
        assert len(runner.calls) == 3

        batch_unit_ref = _rolling_provider_unit_ref(
            operation_ref=run_before.primary_invocation.operation_ref,
            operation_name=f"target-batch-{graph_before_batch.head_generation + 1}",
            attempt_ref=run_before.attempt_ref,
        )
        with runtime._database.write() as connection:
            updated = connection.execute(
                text(
                    "UPDATE ar_provider_units SET status = 'active', completed_at = NULL "
                    "WHERE unit_ref = :unit_ref"
                ),
                {"unit_ref": batch_unit_ref},
            )
            assert updated.rowcount == 1
        assert runtime.owners.agent_runtime.query_bundle_target_proposals(run_before.run_ref) == ()
    finally:
        runtime.close()

    # Restart on a different resident MCP port with NO queued provider outputs.
    restarted, _adapter2 = open_runtime(
        allow_primary=False, allow_dispatch=False, allow_batch=False, crash_on=None, port=8799,
    )
    try:
        run_after = restarted.owners.agent_runtime.query_bundle_stage_run(req_ref)
        assert run_after is not None
        assert run_after.attempt_ref != run_before.attempt_ref
        assert run_after.fence_ref != run_before.fence_ref
        assert run_after.attempt_generation == 2
        assert run_after.primary_invocation.operation_ref == run_before.primary_invocation.operation_ref
        assert run_after.replayable_primary_attempts == ()
        assert run_after.replayable_continuation_attempts == (
            (run_before.attempt_ref, run_before.fence_ref, 1),
        )
        retired_checkpoints = restarted.owners.agent_runtime.query_bundle_inbox_checkpoints_for_attempts(
            run_ref=run_after.run_ref,
            attempts=run_after.replayable_continuation_attempts,
        )
        assert retired_checkpoints
        assert all(
            cp["run_ref"] == run_after.run_ref
            and cp["attempt_ref"] == run_before.attempt_ref
            and cp["fence_ref"] == run_before.fence_ref
            for cp in retired_checkpoints
        )
        _reconcile_revocation_pending_units(restarted, run_after.run_ref)

        for _ in range(16):
            state = restarted.bundle_stage.query_current()
            if state["stage_commit"] is not None:
                break
            assert restarted.bundle_stage.process_once(), restarted.bundle_stage.observation

        final_state = restarted.bundle_stage.query_current()
        assert final_state["stage_commit"] is not None
        assert final_state["stage_commit"]["stage"] == "Bundle"
        assert final_state["stage_commit"]["disposition"] == "completed"
        proposals = restarted.owners.agent_runtime.query_bundle_target_proposals(run_after.run_ref)
        assert len(proposals) == 1
        assert proposals[0].attempt_ref == run_after.attempt_ref
        assert proposals[0].fence_ref == run_after.fence_ref
        final_graph = restarted.owners.research_graph.query_target_graph(req_ref)
        assert final_graph is not None and final_graph.strategy_complete is True
        assert len(runner.calls) == 3
    finally:
        restarted.close()
