from __future__ import annotations

from dataclasses import replace
from itertools import chain
import json
from pathlib import Path
import subprocess

import pytest
from sqlalchemy import text

from meta_research.composition import build_production_runtime
from meta_research.idea_skill import (
    CodexIdeaSkillAdapter,
    IdeaSkillContractError,
    IdeaSkillRequest,
    IdeaSkillUnavailable,
)
from meta_research.bundle_skill import (
    BundleSkillContractError,
    BundleSkillRequest,
    CodexBundleSkillAdapter,
)
from meta_research.bundle_exhaustion import (
    bundle_exhaustion_review_response_document,
)
from meta_research.plan_skill import (
    CodexPlanSkillAdapter,
    PlanSkillContractError,
    PlanSkillRequest,
)
from meta_research.reasoning_skill import (
    CodexReasoningSkillAdapter,
    REASONING_ROOT_SEMANTIC_OPERATION_IDS,
    ReasoningSkillContractError,
)
from meta_research.owners.common import canonical_hash
from meta_research.paths import prepare_data_root
from meta_research.runtime_protection import InhibitorLease, RuntimeProtectionUnavailable
from test_idea_stage_recovery import (
    _ComputeProbe,
    _DraftingProvider,
    _IdeaProvider,
    _confirm_question,
    _runtime,
)
from test_idea_skill_contract import (
    _SequenceRunner,
    _idea_set,
    _review_turn_output,
)
from test_public_bundle_stage import (
    _DeterministicBundleSkill,
    _accept_real_target_root_commit,
    _bundle_runtime,
    _finish_plan_stage,
    _prepare_bundle_request,
)
from test_bundle_exhaustion_owner import _ExhaustionBundleSkill
from test_bundle_skill_adapter import (
    _SequenceRunner as _BundleSequenceRunner,
    _provider_wire_value,
)
from test_public_plan_stage import (
    _DeterministicIdeaSkill,
    _DeterministicPlanSkill,
    _confirm_direct_quest,
    _finish_idea_stage,
    _runtime as _plan_runtime,
)
from test_plan_skill_adapter import _request as _plan_adapter_request
from test_public_advancement_runtime_control import (
    _confirmed_control,
    _execute_control,
)
from test_public_reasoning_stage import (
    _DeterministicReasoningSkill,
    _confirm_deepfetch_quest,
    _reasoning_runtime,
)


def _signed_ceiling_evidence(reason: str) -> dict[str, object]:
    return {
        "schema_ref": "meta-research/provider-hard-ceiling/v1",
        "termination_reason": reason,
        "invocation_hash": canonical_hash({"invocation": reason}),
        "prompt_hash": canonical_hash({"prompt": reason}),
        "output_schema_hash": canonical_hash({"schema": reason}),
        "stdout_hash": canonical_hash({"stdout": reason}),
        "result_file_hash": canonical_hash({"result": reason}),
        "supervisor_receipt_hash": canonical_hash({"receipt": reason}),
    }


def _signed_contract_failure_evidence(
    code: str,
    detail_code: str | None = None,
) -> dict[str, object]:
    return {
        **_signed_ceiling_evidence("completed"),
        "schema_ref": "meta-research/provider-terminal-contract-failure/v1",
        "contract_failure_code": code,
        "contract_failure_detail_code": detail_code or code,
    }


def _runtime_idea_outcome(request) -> dict[str, object]:
    outcome = _idea_set()
    outcome["question_ref"] = request.accepted_question.question_ref
    outcome["context_pack_ref"] = request.context_pack_ref
    candidates = outcome["candidates"]
    assert isinstance(candidates, list)
    candidate = candidates[0]
    assert isinstance(candidate, dict)
    evidence_boundary = candidate["evidence_boundary"]
    assert isinstance(evidence_boundary, dict)
    accepted_evidence_refs = request.context_pack.get("accepted_evidence_refs")
    assert isinstance(accepted_evidence_refs, list)
    evidence_boundary["accepted_evidence_refs"] = list(accepted_evidence_refs)
    return outcome


def _provider_terminal_rows(runtime, operation_ref: str) -> dict[str, object]:
    with runtime._database.read() as connection:
        units = tuple(
            tuple(row)
            for row in connection.execute(
                text(
                    "SELECT unit_ref, status, started_at, completed_at FROM "
                    "ar_provider_units WHERE operation_ref = :operation_ref "
                    "ORDER BY unit_ref"
                ),
                {"operation_ref": operation_ref},
            ).all()
        )
        responsibilities = tuple(
            tuple(row)
            for row in connection.execute(
                text(
                    "SELECT responsibility_ref, status, boundary, updated_at, "
                    "finished_at FROM ar_execution_responsibilities WHERE "
                    "operation_ref = :operation_ref ORDER BY responsibility_ref"
                ),
                {"operation_ref": operation_ref},
            ).all()
        )
        receipts = tuple(
            tuple(row)
            for row in connection.execute(
                text(
                    "SELECT responsibility_ref, boundary, recorded_at FROM "
                    "ar_runtime_boundary_receipts WHERE operation_ref = "
                    ":operation_ref ORDER BY responsibility_ref"
                ),
                {"operation_ref": operation_ref},
            ).all()
        )
    return {
        "units": units,
        "responsibilities": responsibilities,
        "receipts": receipts,
    }


def _transport_contract_codex(
    executable: Path,
    call_counter: Path,
    *,
    failure_detail: str,
) -> Path:
    if failure_detail == "codex_output_invalid":
        result = "not-json"
        events = [
            {"type": "thread.started", "thread_id": "plan-transport-primary"}
        ]
    elif failure_detail == "codex_native_session_missing":
        result = "{}"
        events = []
    elif failure_detail == "codex_native_session_mismatch":
        result = "{}"
        events = [
            {"type": "thread.started", "thread_id": "plan-transport-primary"},
            {"type": "thread.started", "thread_id": "alien-plan-session"},
        ]
    else:  # pragma: no cover - protects the test fixture itself
        raise AssertionError(failure_detail)
    encoded_events = repr("\n".join(json.dumps(event) for event in events))
    executable.write_text(
        "#!/usr/bin/python3\n"
        "import sys\n"
        "from pathlib import Path\n"
        "if '--version' in sys.argv:\n"
        "    print('codex-plan-transport-contract-test 1')\n"
        "    raise SystemExit(0)\n"
        "sys.stdin.buffer.read()\n"
        f"counter = Path({str(call_counter)!r})\n"
        "count = int(counter.read_text()) if counter.exists() else 0\n"
        "counter.write_text(str(count + 1))\n"
        "args = sys.argv[1:]\n"
        "result_path = Path(args[args.index('--output-last-message') + 1])\n"
        f"result_path.write_text({result!r}, encoding='utf-8')\n"
        f"print({encoded_events})\n",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    return executable


def _current_plan_skill_request(runtime, cycle_ref: str):
    request = runtime.owners.advancement_engine.query_plan_stage_request(cycle_ref)
    assert request is not None and request.accepted_idea_set is not None
    run = runtime.owners.agent_runtime.query_plan_stage_run(request.request_ref)
    assert run is not None
    return request, run, PlanSkillRequest(
        stage_request_ref=request.request_ref,
        cycle_ref=request.cycle_ref,
        question_ref=request.accepted_question.question_ref,
        idea_set_ref=request.accepted_idea_set.outcome_ref,
        context_pack_ref=request.context_pack_ref,
        context_pack_hash=request.context_pack_hash,
        context_pack=request.context_pack,
        accepted_question_content=(
            runtime.plan_stage._accepted_question_content(  # type: ignore[attr-defined]
                request.accepted_question
            )
        ),
        accepted_idea_set=request.accepted_idea_set.idea_set,
        root_session_ref=run.root_session_ref,
        submission_revision=run.attempt_generation,
        runtime_binding=run.runtime_binding,
        job_ref=run.primary_invocation.operation_ref,
    )


class _ResidentSequenceRunner(_SequenceRunner):
    """Durable JSONL fixture usable by resident-MCP production adapters."""

    def __call__(
        self,
        argv: list[str],
        prompt: str,
        timeout: float,
        environment: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        del environment
        schema_path = Path(argv[argv.index("--output-schema") + 1])
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        logical_output = next(self._outputs)
        wire_output = _provider_wire_value(logical_output, schema)
        assert isinstance(wire_output, dict)
        self._outputs = chain((wire_output,), self._outputs)
        completed = super().__call__(argv, prompt, timeout)
        review_events: list[dict[str, object]] = []
        reviewer_agent_ref = logical_output.get("reviewer_agent_ref")
        if isinstance(reviewer_agent_ref, str):
            if self._emit_review_spawn:
                review_events.append(
                    {
                        "type": "item.completed",
                        "item": {
                            "id": "collab-spawn:resident",
                            "type": "collab_tool_call",
                            "tool": "spawn_agent",
                            "sender_thread_id": "codex-primary:1",
                            "receiver_thread_ids": [reviewer_agent_ref],
                            "agents_states": {
                                reviewer_agent_ref: {"status": "pending_init"}
                            },
                            "status": "completed",
                        },
                    }
                )
            if self._emit_review_wait:
                review_events.append(
                    {
                        "type": "item.completed",
                        "item": {
                            "id": "collab-wait:resident",
                            "type": "collab_tool_call",
                            "tool": "wait",
                            "sender_thread_id": "codex-primary:1",
                            "receiver_thread_ids": [reviewer_agent_ref],
                            "agents_states": {
                                reviewer_agent_ref: {"status": "completed"}
                            },
                            "status": "completed",
                        },
                    }
                )
        semantic_events = [
            {
                "type": "item.completed",
                "item": {
                    "id": f"mcp:{operation_id}",
                    "type": "mcp_tool_call",
                    "server": "meta_research",
                    "tool": operation_id,
                    "status": "completed",
                    "result": {"isError": False},
                },
            }
            for operation_id in REASONING_ROOT_SEMANTIC_OPERATION_IDS
        ] if not getattr(self, "_suppress_semantic_once", False) else []
        return subprocess.CompletedProcess(
            completed.args,
            completed.returncode,
            stdout="\n".join(
                (
                    completed.stdout,
                    *(json.dumps(event) for event in review_events),
                    *(json.dumps(event) for event in semantic_events),
                )
            ),
            stderr=completed.stderr,
        )

    def run_job(
        self,
        job_ref: str,
        argv: list[str],
        prompt: str,
        timeout: float,
        environment: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        del job_ref
        return self(argv, prompt, timeout, environment)


class _SelectiveSemanticSequenceRunner(_ResidentSequenceRunner):
    def __init__(
        self,
        outputs: list[dict[str, object]],
        *,
        missing_semantic_call: int,
    ) -> None:
        super().__init__(outputs)
        self._missing_semantic_call = missing_semantic_call
        self._semantic_call = 0

    def __call__(
        self,
        argv: list[str],
        prompt: str,
        timeout: float,
        environment: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        call = self._semantic_call
        self._semantic_call += 1
        if call == self._missing_semantic_call:
            self._suppress_semantic_once = True
            try:
                return super().__call__(argv, prompt, timeout, environment)
            finally:
                self._suppress_semantic_once = False
        return super().__call__(argv, prompt, timeout, environment)


class _CeilingIdeaProvider(_IdeaProvider):
    def __init__(self) -> None:
        super().__init__()
        self.generate_calls = 0

    def generate_draft(self, request: IdeaSkillRequest):
        self.generate_calls += 1
        raise IdeaSkillUnavailable(
            "codex_operation_timeout",
            recovery_checkpoint=_signed_ceiling_evidence("timeout"),
        )


class _TerminalFailureIdeaProvider(_IdeaProvider):
    def __init__(self, reason: str = "completed") -> None:
        super().__init__()
        self.reason = reason
        self.generate_calls = 0

    def generate_draft(self, request: IdeaSkillRequest):
        self.generate_calls += 1
        raise IdeaSkillUnavailable(
            "codex_operation_failed",
            recovery_checkpoint=_signed_ceiling_evidence(self.reason),
        )


class _SealedReviewTraceFailureIdeaProvider(_IdeaProvider):
    def __init__(self) -> None:
        super().__init__()
        self.review_calls = 0

    def review_draft(self, request, draft):
        del request, draft
        self.review_calls += 1
        raise IdeaSkillUnavailable(
            "codex_child_review_spawn_invalid",
            recovery_checkpoint=_signed_contract_failure_evidence(
                "codex_child_review_spawn_invalid"
            ),
        )


class _SealedMaterialContractFailureIdeaProvider(_IdeaProvider):
    def __init__(self) -> None:
        super().__init__()
        self.review_calls = 0
        self.checkpoint_calls = 0

    def review_draft(self, request, draft):
        self.review_calls += 1
        result = super().review_draft(request, draft)
        return replace(
            result,
            findings=(
                {
                    "finding_id": "finding-material",
                    "category": "falsifiability",
                    "message": "必须形成实质修订。",
                },
            ),
            dispositions=(
                {
                    "finding_id": "finding-material",
                    "action": "revised",
                    "rationale": "声称已修订，但 Outcome 未改变。",
                },
            ),
        )

    def terminal_contract_failure_checkpoint(
        self,
        *,
        job_ref,
        operation_name,
        native_session_ref,
        failure_code,
        detail_code,
    ):
        assert job_ref
        assert operation_name == "review"
        assert native_session_ref
        assert failure_code == "idea_review_result_contract_invalid"
        assert detail_code == "review_revision_not_material"
        self.checkpoint_calls += 1
        return _signed_contract_failure_evidence(failure_code, detail_code)


class _SealedMaterialContractFailurePlanProvider(_DeterministicPlanSkill):
    def __init__(self) -> None:
        super().__init__(no_gap=False)
        self.review_calls = 0

    def review_draft(self, request, draft):
        self.review_calls += 1
        result = super().review_draft(request, draft)
        return replace(
            result,
            findings=(
                {
                    "finding_id": "plan-material",
                    "category": "evidence_boundary",
                    "message": "必须形成实质修订。",
                },
            ),
            dispositions=(
                {
                    "finding_id": "plan-material",
                    "action": "revised",
                    "rationale": "声称修订但 Plan 未改变。",
                },
            ),
        )

    def terminal_contract_failure_checkpoint(self, **values):
        assert values["operation_name"] == "review"
        assert values["failure_code"] == "plan_review_result_contract_invalid"
        assert values["detail_code"] == "plan_review_revision_not_material"
        return _signed_contract_failure_evidence(
            values["failure_code"], values["detail_code"]
        )


class _SealedMaterialContractFailureBundleProvider(_DeterministicBundleSkill):
    def __init__(self) -> None:
        self.review_calls = 0

    def review_draft(self, request, draft):
        self.review_calls += 1
        result = super().review_draft(request, draft)
        return replace(
            result,
            findings=(
                {
                    "finding_id": "bundle-material",
                    "category": "owner_boundary",
                    "message": "必须形成实质修订。",
                },
            ),
            dispositions=(
                {
                    "finding_id": "bundle-material",
                    "action": "revised",
                    "rationale": "声称修订但 TargetPlan 未改变。",
                },
            ),
        )

    def terminal_contract_failure_checkpoint(self, **values):
        assert values["operation_name"] == "review"
        assert values["failure_code"] == "bundle_review_result_contract_invalid"
        assert values["detail_code"] == (
            "target_plan_review_revision_not_material"
        )
        return _signed_contract_failure_evidence(
            values["failure_code"], values["detail_code"]
        )


class _SealedMaterialContractFailureReasoningProvider(
    _DeterministicReasoningSkill
):
    def __init__(self) -> None:
        super().__init__()
        self.review_calls = 0

    def review_draft(self, request, draft):
        self.review_calls += 1
        result = super().review_draft(request, draft)
        return replace(
            result,
            findings=(
                {
                    "finding_id": "reasoning-material",
                    "category": "owner_boundary",
                    "message": "必须形成实质修订。",
                },
            ),
            dispositions=(
                {
                    "finding_id": "reasoning-material",
                    "action": "revised",
                    "rationale": "声称修订但 Reasoning output 未改变。",
                },
            ),
        )

    def terminal_contract_failure_checkpoint(self, **values):
        assert values["operation_name"] == "review"
        assert values["failure_code"] == (
            "reasoning_review_result_contract_invalid"
        )
        assert values["detail_code"] == "reasoning_review_revision_invalid"
        return _signed_contract_failure_evidence(
            values["failure_code"], values["detail_code"]
        )


class _CeilingPlanProvider(_DeterministicPlanSkill):
    def __init__(self) -> None:
        super().__init__(no_gap=False)
        self.generate_calls = 0

    def generate_draft(self, request):
        self.generate_calls += 1
        raise IdeaSkillUnavailable(
            "codex_operation_output_limit",
            recovery_checkpoint=_signed_ceiling_evidence("output_limit"),
        )


class _CeilingBundleProvider(_DeterministicBundleSkill):
    def __init__(self) -> None:
        self.generate_calls = 0

    def generate_draft(self, request):
        self.generate_calls += 1
        raise IdeaSkillUnavailable(
            "codex_operation_timeout",
            recovery_checkpoint=_signed_ceiling_evidence("timeout"),
        )


class _DirectContractIdeaProvider(_IdeaProvider):
    def __init__(self) -> None:
        super().__init__()
        self.generate_calls = 0

    def generate_draft(self, request):
        del request
        self.generate_calls += 1
        raise IdeaSkillContractError("idea_provider_contract_error")


class _DirectContractPlanProvider(_DeterministicPlanSkill):
    def __init__(self) -> None:
        super().__init__(no_gap=False)
        self.generate_calls = 0

    def generate_draft(self, request):
        del request
        self.generate_calls += 1
        raise PlanSkillContractError("plan_provider_contract_error")


class _DirectContractBundleProvider(_DeterministicBundleSkill):
    def __init__(self) -> None:
        self.generate_calls = 0

    def generate_draft(self, request):
        del request
        self.generate_calls += 1
        raise BundleSkillContractError("bundle_provider_contract_error")


class _DirectContractReasoningProvider(_DeterministicReasoningSkill):
    def __init__(self) -> None:
        super().__init__()
        self.generate_calls = 0

    def generate_draft(self, request):
        del request
        self.generate_calls += 1
        raise ReasoningSkillContractError("reasoning_provider_contract_error")


class _UnavailableInhibitor:
    kind = "test_unavailable_inhibitor"

    def __init__(self) -> None:
        self.available = True
        self.active: set[str] = set()

    def acquire(self, *, holder_ref: str, reason: str):
        del reason
        if not self.available:
            raise RuntimeProtectionUnavailable("power_inhibitor_acquisition_failed")
        self.active.add(holder_ref)
        return InhibitorLease(
            holder_ref=holder_ref,
            backend=self.kind,
            scope="sleep",
            acquired_at=1.0,
            native_holder_ref="test-native:" + holder_ref,
        )

    def is_confirmed(self, lease) -> bool:
        return lease.holder_ref in self.active

    def query_hold(self, lease) -> str:
        return "confirmed" if lease.holder_ref in self.active else "absent"

    def release(self, lease) -> None:
        self.active.discard(lease.holder_ref)


def test_idea_signed_hard_ceiling_durably_fences_attempt_without_terminal_run(
    tmp_path: Path,
) -> None:
    data_root = prepare_data_root(tmp_path / "idea-hard-ceiling")
    provider = _CeilingIdeaProvider()
    runtime = _runtime(data_root, provider)
    try:
        completed = _confirm_question(runtime)
        runtime.idea_stage.start("idea-hard-ceiling-start")

        assert not runtime.idea_stage.process_once()
        current = runtime.idea_stage.query_current()
        run = current["run"]
        assert run["status"] == "suspended_fenced"
        assert run["fence_status"] == "revoked"
        assert run["blocker"] == {
            "status": "durable",
            "reason": {"code": "codex_operation_timeout"},
        }
        checkpoint = run["recovery_checkpoint"]
        assert checkpoint["checkpoint"]["provider_exit"] == (
            _signed_ceiling_evidence("timeout")
        )
        assert checkpoint["checkpoint"]["attempt_ref"] == run["attempt_ref"]
        assert checkpoint["checkpoint"]["fence_ref"] == run["fence_ref"]
        assert current["stage_commit"] is None

        managed = runtime.owners.agent_runtime.query_managed_run(run["run_ref"])
        assert managed is not None
        assert managed["status"] == "suspended_fenced"
        assert managed["terminal_reason"] == "codex_operation_timeout"
        assert managed["safe_point"] == checkpoint

        # A later worker pass observes the durable fence and cannot replay the
        # sealed provider operation on the same Attempt.
        assert not runtime.idea_stage.process_once()
        assert provider.generate_calls == 1
        assert runtime.idea_stage.query_current()["stage_commit"] is None
        assert completed["cycle_ref"] == current["stage_run_request"]["cycle_ref"]
    finally:
        runtime.close()


def test_idea_signed_terminal_failure_is_fenced_without_replay(
    tmp_path: Path,
) -> None:
    provider = _TerminalFailureIdeaProvider()
    runtime = _runtime(
        prepare_data_root(tmp_path / "idea-terminal-failure"), provider
    )
    try:
        _confirm_question(runtime)
        runtime.idea_stage.start("idea-terminal-failure-start")

        assert not runtime.idea_stage.process_once()
        run = runtime.idea_stage.query_current()["run"]
        assert run["status"] == "suspended_fenced"
        assert run["fence_status"] == "revoked"
        assert run["blocker"] == {
            "status": "durable",
            "reason": {"code": "codex_operation_failed"},
        }
        assert run["recovery_checkpoint"]["checkpoint"]["provider_exit"] == (
            _signed_ceiling_evidence("completed")
        )

        assert not runtime.idea_stage.process_once()
        assert provider.generate_calls == 1
    finally:
        runtime.close()


def test_idea_sealed_review_trace_failure_is_fenced_without_replay(
    tmp_path: Path,
) -> None:
    provider = _SealedReviewTraceFailureIdeaProvider()
    runtime = _runtime(
        prepare_data_root(tmp_path / "idea-review-trace-failure"), provider
    )
    try:
        _confirm_question(runtime)
        runtime.idea_stage.start("idea-review-trace-failure-start")

        assert runtime.idea_stage.process_once()
        primary_complete = runtime.idea_stage.query_current()["run"]
        assert primary_complete["primary_draft_checkpoint"]["status"] == "recorded"

        assert not runtime.idea_stage.process_once()
        run = runtime.idea_stage.query_current()["run"]
        assert run["status"] == "suspended_fenced"
        assert run["fence_status"] == "revoked"
        assert run["blocker"] == {
            "status": "durable",
            "reason": {"code": "codex_child_review_spawn_invalid"},
        }
        assert run["recovery_checkpoint"]["checkpoint"]["provider_exit"] == (
            _signed_contract_failure_evidence(
                "codex_child_review_spawn_invalid"
            )
        )

        assert not runtime.idea_stage.process_once()
        assert provider.review_calls == 1
    finally:
        runtime.close()


def test_idea_sealed_material_contract_failure_is_fenced_without_replay(
    tmp_path: Path,
) -> None:
    provider = _SealedMaterialContractFailureIdeaProvider()
    runtime = _runtime(
        prepare_data_root(tmp_path / "idea-material-contract-failure"), provider
    )
    try:
        _confirm_question(runtime)
        runtime.idea_stage.start("idea-material-contract-failure-start")

        assert runtime.idea_stage.process_once()
        assert not runtime.idea_stage.process_once()
        run = runtime.idea_stage.query_current()["run"]
        assert run["status"] == "suspended_fenced"
        assert run["fence_status"] == "revoked"
        assert run["blocker"] == {
            "status": "durable",
            "reason": {"code": "idea_review_result_contract_invalid"},
        }
        assert run["recovery_checkpoint"]["checkpoint"]["provider_exit"] == (
            _signed_contract_failure_evidence(
                "idea_review_result_contract_invalid",
                "review_revision_not_material",
            )
        )

        assert not runtime.idea_stage.process_once()
        assert provider.review_calls == 1
        assert provider.checkpoint_calls == 1
    finally:
        runtime.close()


def test_plan_post_result_contract_failure_is_terminal(
    tmp_path: Path,
) -> None:
    provider = _SealedMaterialContractFailurePlanProvider()
    runtime = _plan_runtime(
        tmp_path / "plan-material-contract-failure",
        idea_skill=_DeterministicIdeaSkill(),
        plan_skill=provider,
    )
    try:
        _confirm_direct_quest(runtime)
        _finish_idea_stage(runtime)
        for _step in range(5):
            changed = runtime.plan_stage.process_once()
            if provider.review_calls:
                assert not changed
                break
            assert changed
        blocked = runtime.plan_stage.query_current()["run"]
        assert blocked["status"] == "suspended_fenced"
        assert blocked["blocker"]["reason"] == {
            "code": "plan_review_result_contract_invalid"
        }
        assert not runtime.plan_stage.process_once()
        assert provider.review_calls == 1
    finally:
        runtime.close()


def test_bundle_post_result_contract_failure_is_terminal(
    tmp_path: Path,
) -> None:
    provider = _SealedMaterialContractFailureBundleProvider()
    runtime = _bundle_runtime(
        tmp_path / "bundle-material-contract-failure",
        bundle_skill_provider=provider,
    )
    try:
        _prepare_bundle_request(runtime)
        for _step in range(4):
            changed = runtime.bundle_stage.process_once()
            if provider.review_calls:
                assert not changed
                break
            assert changed
        blocked = runtime.bundle_stage.query_current()["run"]
        assert blocked["status"] == "suspended_fenced"
        assert blocked["blocker"]["reason"] == {
            "code": "bundle_review_result_contract_invalid"
        }
        assert not runtime.bundle_stage.process_once()
        assert provider.review_calls == 1
    finally:
        runtime.close()


def test_reasoning_post_result_contract_failure_is_terminal(
    tmp_path: Path,
) -> None:
    provider = _SealedMaterialContractFailureReasoningProvider()
    runtime = _reasoning_runtime(
        tmp_path / "reasoning-material-contract-failure",
        reasoning_skill=provider,
    )
    try:
        _confirm_deepfetch_quest(runtime)
        _finish_idea_stage(runtime)
        for _step in range(5):
            changed = runtime.reasoning_stage.process_once()
            if provider.review_calls:
                assert not changed
                break
            assert changed
        blocked = runtime.reasoning_stage.query_current()["run"]
        assert blocked["status"] == "suspended_fenced"
        assert blocked["blocker"]["reason"] == {
            "code": "reasoning_review_result_contract_invalid"
        }
        assert not runtime.reasoning_stage.process_once()
        assert provider.review_calls == 1
    finally:
        runtime.close()


@pytest.mark.parametrize("stage", ["idea", "plan", "bundle", "reasoning"])
def test_direct_provider_contract_error_remains_transient(
    tmp_path: Path,
    stage: str,
) -> None:
    if stage == "idea":
        provider = _DirectContractIdeaProvider()
        runtime = _runtime(prepare_data_root(tmp_path / stage), provider)
        _confirm_question(runtime)
        runtime.idea_stage.start("idea-direct-contract-error")
        worker = runtime.idea_stage
    elif stage == "plan":
        provider = _DirectContractPlanProvider()
        runtime = _plan_runtime(
            tmp_path / stage,
            idea_skill=_DeterministicIdeaSkill(),
            plan_skill=provider,
        )
        _confirm_direct_quest(runtime)
        _finish_idea_stage(runtime)
        worker = runtime.plan_stage
    elif stage == "bundle":
        provider = _DirectContractBundleProvider()
        runtime = _bundle_runtime(
            tmp_path / stage,
            bundle_skill_provider=provider,
        )
        _prepare_bundle_request(runtime)
        worker = runtime.bundle_stage
    else:
        provider = _DirectContractReasoningProvider()
        runtime = _reasoning_runtime(
            tmp_path / stage,
            reasoning_skill=provider,
        )
        _confirm_deepfetch_quest(runtime)
        _finish_idea_stage(runtime)
        worker = runtime.reasoning_stage

    try:
        for _step in range(5):
            changed = worker.process_once()
            if provider.generate_calls:
                assert not changed
                break
            assert changed
        else:
            raise AssertionError(f"{stage} provider boundary was not reached")

        assert worker.transient_error == f"{stage}_provider_contract_error"
        current = worker.query_current()
        assert current["run"]["status"] == "admitted"
        assert current["run"]["recovery_checkpoint"] is None
    finally:
        runtime.close()


@pytest.mark.parametrize(
    ("phase", "failure_code", "detail_code", "expected_calls"),
    [
        (
            "primary",
            "idea_primary_result_contract_invalid",
            "codex_outcome_invalid",
            1,
        ),
        (
            "review",
            "idea_review_result_contract_invalid",
            "codex_review_invalid",
            2,
        ),
    ],
)
def test_production_adapter_invalid_shape_is_terminal_without_replay(
    tmp_path: Path,
    phase: str,
    failure_code: str,
    detail_code: str,
    expected_calls: int,
) -> None:
    data_root = prepare_data_root(tmp_path / f"idea-{phase}-shape-failure")
    workspace = tmp_path / f"idea-{phase}-shape-provider"
    outputs: list[dict[str, object]] = []
    runner = _SequenceRunner(outputs)
    adapter = CodexIdeaSkillAdapter(workspace, process_runner=runner)
    runtime = _runtime(data_root, adapter)  # type: ignore[arg-type]
    try:
        completed = _confirm_question(runtime)
        runtime.idea_stage.start(f"idea-{phase}-shape-failure-start")
        request = runtime.owners.advancement_engine.query_idea_stage_request(
            completed["cycle_ref"]
        )
        assert request is not None
        if phase == "primary":
            outputs.append({})
        else:
            outputs.extend(
                [
                    {"outcome": _runtime_idea_outcome(request)},
                    {"reviewer_agent_ref": "codex-reviewer:shape-invalid"},
                ]
            )
            assert runtime.idea_stage.process_once()

        assert not runtime.idea_stage.process_once()
        blocked = runtime.idea_stage.query_current()["run"]
        assert blocked["status"] == "suspended_fenced"
        assert blocked["blocker"] == {
            "status": "durable",
            "reason": {"code": failure_code},
        }
        provider_exit = blocked["recovery_checkpoint"]["checkpoint"][
            "provider_exit"
        ]
        assert provider_exit["contract_failure_code"] == failure_code
        assert provider_exit["contract_failure_detail_code"] == detail_code

        run = runtime.owners.agent_runtime.query_idea_stage_run(
            request.request_ref
        )
        assert run is not None
        invocation = (
            run.primary_invocation if phase == "primary" else run.review_invocation
        )
        operation_ref = invocation.operation_ref
        terminal_rows = _provider_terminal_rows(runtime, operation_ref)
        assert len(terminal_rows["units"]) == 1
        assert terminal_rows["units"][0][1] == "revoked"
        assert terminal_rows["units"][0][3] is not None
        assert len(terminal_rows["responsibilities"]) == 1
        assert terminal_rows["responsibilities"][0][1:3] == (
            "finished",
            "permanent_fence",
        )
        assert len(runner.calls) == expected_calls

        assert not runtime.idea_stage.process_once()
        assert _provider_terminal_rows(runtime, operation_ref) == terminal_rows
        assert len(runner.calls) == expected_calls
    finally:
        runtime.close()

    no_replay = _SequenceRunner([])
    restarted = _runtime(
        data_root,
        CodexIdeaSkillAdapter(workspace, process_runner=no_replay),
    )  # type: ignore[arg-type]
    try:
        assert not restarted.idea_stage.process_once()
        assert _provider_terminal_rows(restarted, operation_ref) == terminal_rows
        assert no_replay.calls == []
    finally:
        restarted.close()


def test_production_adapter_sealed_review_trace_failure_never_replays(
    tmp_path: Path,
) -> None:
    data_root = prepare_data_root(tmp_path / "production-review-trace-failure")
    workspace = tmp_path / "production-review-trace-provider"
    outputs: list[dict[str, object]] = []
    runner = _SequenceRunner(
        outputs,
        emit_review_spawn=False,
        emit_review_wait=False,
    )
    adapter = CodexIdeaSkillAdapter(workspace, process_runner=runner)
    runtime = _runtime(data_root, adapter)  # type: ignore[arg-type]
    try:
        completed = _confirm_question(runtime)
        runtime.idea_stage.start("production-review-trace-failure-start")
        request = runtime.owners.advancement_engine.query_idea_stage_request(
            completed["cycle_ref"]
        )
        assert request is not None
        outcome = _runtime_idea_outcome(request)
        outputs.extend(
            [
                {"outcome": outcome},
                _review_turn_output(final_outcome=outcome),
            ]
        )

        assert runtime.idea_stage.process_once()
        assert not runtime.idea_stage.process_once()
        blocked = runtime.idea_stage.query_current()["run"]
        assert blocked["status"] == "suspended_fenced"
        assert blocked["blocker"] == {
            "status": "durable",
            "reason": {"code": "codex_child_review_spawn_invalid"},
        }
        run = runtime.owners.agent_runtime.query_idea_stage_run(
            request.request_ref
        )
        assert run is not None
        operation_ref = run.review_invocation.operation_ref
        terminal_rows = _provider_terminal_rows(runtime, operation_ref)
        assert len(terminal_rows["units"]) == 1
        assert terminal_rows["units"][0][1] == "revoked"
        assert terminal_rows["units"][0][3] is not None
        assert len(terminal_rows["responsibilities"]) == 1
        assert terminal_rows["responsibilities"][0][1:3] == (
            "finished",
            "permanent_fence",
        )
        assert len(runner.calls) == 2

        assert not runtime.idea_stage.process_once()
        assert _provider_terminal_rows(runtime, operation_ref) == terminal_rows
        assert len(runner.calls) == 2
    finally:
        runtime.close()

    no_replay = _SequenceRunner([])
    restarted_adapter = CodexIdeaSkillAdapter(
        workspace,
        process_runner=no_replay,
    )
    restarted = _runtime(data_root, restarted_adapter)  # type: ignore[arg-type]
    try:
        assert not restarted.idea_stage.process_once()
        assert _provider_terminal_rows(restarted, operation_ref) == terminal_rows
        assert no_replay.calls == []
    finally:
        restarted.close()


def test_production_adapter_primary_phase_violation_is_owner_terminal(
    tmp_path: Path,
) -> None:
    data_root = prepare_data_root(tmp_path / "production-primary-phase-failure")
    outputs: list[dict[str, object]] = []
    runner = _SequenceRunner(outputs, emit_primary_review_trace=True)
    adapter = CodexIdeaSkillAdapter(
        tmp_path / "production-primary-phase-provider",
        process_runner=runner,
    )
    runtime = _runtime(data_root, adapter)  # type: ignore[arg-type]
    try:
        completed = _confirm_question(runtime)
        runtime.idea_stage.start("production-primary-phase-failure-start")
        request = runtime.owners.advancement_engine.query_idea_stage_request(
            completed["cycle_ref"]
        )
        assert request is not None
        outputs.append({"outcome": _runtime_idea_outcome(request)})

        assert not runtime.idea_stage.process_once()
        blocked = runtime.idea_stage.query_current()["run"]
        assert blocked["status"] == "suspended_fenced"
        assert blocked["blocker"] == {
            "status": "durable",
            "reason": {"code": "codex_primary_review_phase_invalid"},
        }
        assert not runtime.idea_stage.process_once()
        assert len(runner.calls) == 1
    finally:
        runtime.close()


@pytest.mark.parametrize(
    "detail_code",
    [
        "codex_output_invalid",
        "codex_native_session_missing",
        "codex_native_session_mismatch",
    ],
)
def test_plan_sealed_transport_contract_failure_is_terminal_without_replay(
    tmp_path: Path,
    detail_code: str,
) -> None:
    data_path = tmp_path / f"plan-transport-{detail_code}"
    workspace = tmp_path / f"plan-transport-provider-{detail_code}"
    call_counter = tmp_path / f"plan-transport-calls-{detail_code}"
    executable = _transport_contract_codex(
        tmp_path / f"codex-plan-transport-{detail_code}",
        call_counter,
        failure_detail=detail_code,
    )
    runtime = _plan_runtime(
        data_path,
        idea_skill=_DeterministicIdeaSkill(),
        plan_skill=CodexPlanSkillAdapter(workspace, executable=str(executable)),
    )
    try:
        quest = _confirm_direct_quest(runtime)
        _finish_idea_stage(runtime)
        for _step in range(10):
            changed = runtime.plan_stage.process_once()
            current = runtime.plan_stage.query_current()
            if (
                current["run"] is not None
                and current["run"]["status"] == "suspended_fenced"
            ):
                assert not changed
                break
        else:
            raise AssertionError("Plan transport failure did not permanently fence")

        blocked = current["run"]
        assert blocked["blocker"]["reason"] == {
            "code": "plan_primary_result_contract_invalid"
        }
        provider_exit = blocked["recovery_checkpoint"]["checkpoint"][
            "provider_exit"
        ]
        assert provider_exit["contract_failure_code"] == (
            "plan_primary_result_contract_invalid"
        )
        assert provider_exit["contract_failure_detail_code"] == detail_code
        request = runtime.owners.advancement_engine.query_plan_stage_request(
            str(quest["cycle_ref"])
        )
        assert request is not None
        run = runtime.owners.agent_runtime.query_plan_stage_run(request.request_ref)
        assert run is not None
        operation_ref = run.primary_invocation.operation_ref
        terminal_rows = _provider_terminal_rows(runtime, operation_ref)
        operation = next(workspace.glob("provider-operations/*/primary"))
        assert (operation / "exit.json").is_file()
        assert not (operation / "completed.json").exists()
        assert call_counter.read_text(encoding="utf-8") == "1"

        assert not runtime.plan_stage.process_once()
        assert _provider_terminal_rows(runtime, operation_ref) == terminal_rows
        assert call_counter.read_text(encoding="utf-8") == "1"
    finally:
        runtime.close()

    restarted = _plan_runtime(
        data_path,
        idea_skill=_DeterministicIdeaSkill(),
        plan_skill=CodexPlanSkillAdapter(workspace, executable=str(executable)),
    )
    try:
        assert not restarted.plan_stage.process_once()
        assert _provider_terminal_rows(restarted, operation_ref) == terminal_rows
        assert call_counter.read_text(encoding="utf-8") == "1"
    finally:
        restarted.close()


@pytest.mark.parametrize(
    "detail_code",
    [
        "codex_output_invalid",
        "codex_native_session_missing",
        "codex_native_session_mismatch",
    ],
)
def test_plan_restart_terminalizes_preexisting_raw_transport_failure(
    tmp_path: Path,
    detail_code: str,
) -> None:
    data_path = tmp_path / f"plan-transport-crash-{detail_code}"
    workspace = tmp_path / f"plan-transport-crash-provider-{detail_code}"
    call_counter = tmp_path / f"plan-transport-crash-calls-{detail_code}"
    executable = _transport_contract_codex(
        tmp_path / f"codex-plan-transport-crash-{detail_code}",
        call_counter,
        failure_detail=detail_code,
    )
    adapter = CodexPlanSkillAdapter(workspace, executable=str(executable))
    runtime = _plan_runtime(
        data_path,
        idea_skill=_DeterministicIdeaSkill(),
        plan_skill=adapter,
    )
    try:
        quest = _confirm_direct_quest(runtime)
        _finish_idea_stage(runtime)
        for _step in range(4):
            if runtime.plan_stage.query_current()["run"] is not None:
                break
            assert runtime.plan_stage.process_once()
        request, run, skill_request = _current_plan_skill_request(
            runtime,
            str(quest["cycle_ref"]),
        )
        with pytest.raises(
            IdeaSkillUnavailable,
            match="plan_primary_result_contract_invalid",
        ) as first_failure:
            adapter.generate_draft(skill_request)
        first_checkpoint = first_failure.value.recovery_checkpoint
        assert first_checkpoint is not None
        assert first_checkpoint["contract_failure_detail_code"] == detail_code
        operation = next(workspace.glob("provider-operations/*/primary"))
        exit_marker = json.loads(
            (operation / "exit.json").read_text(encoding="utf-8")
        )
        assert not (operation / "completed.json").exists()
        assert call_counter.read_text(encoding="utf-8") == "1"
        operation_ref = run.primary_invocation.operation_ref
        assert _provider_terminal_rows(runtime, operation_ref) == {
            "units": (),
            "responsibilities": (),
            "receipts": (),
        }
    finally:
        runtime.close()

    restarted = _plan_runtime(
        data_path,
        idea_skill=_DeterministicIdeaSkill(),
        plan_skill=CodexPlanSkillAdapter(workspace, executable=str(executable)),
    )
    try:
        assert not restarted.plan_stage.process_once()
        blocked = restarted.plan_stage.query_current()["run"]
        assert blocked["status"] == "suspended_fenced"
        provider_exit = blocked["recovery_checkpoint"]["checkpoint"][
            "provider_exit"
        ]
        assert provider_exit == first_checkpoint
        for field in (
            "invocation_hash",
            "prompt_hash",
            "output_schema_hash",
            "stdout_hash",
            "result_file_hash",
            "supervisor_receipt_hash",
        ):
            assert provider_exit[field] == exit_marker[field]
        terminal_rows = _provider_terminal_rows(restarted, operation_ref)
        assert any(row[1] == "revoked" for row in terminal_rows["units"])
        assert call_counter.read_text(encoding="utf-8") == "1"

        assert not restarted.plan_stage.process_once()
        assert _provider_terminal_rows(restarted, operation_ref) == terminal_rows
        assert call_counter.read_text(encoding="utf-8") == "1"
    finally:
        restarted.close()


@pytest.mark.parametrize("tamper_kind", ["missing", "hash_drift"])
def test_plan_raw_transport_proof_rejects_missing_or_changed_artifact(
    tmp_path: Path,
    tamper_kind: str,
) -> None:
    workspace = tmp_path / f"plan-transport-tamper-{tamper_kind}"
    executable = _transport_contract_codex(
        tmp_path / f"codex-plan-transport-tamper-{tamper_kind}",
        tmp_path / f"plan-transport-tamper-calls-{tamper_kind}",
        failure_detail="codex_output_invalid",
    )
    adapter = CodexPlanSkillAdapter(workspace, executable=str(executable))
    request = _plan_adapter_request(
        runtime_binding=adapter.runtime_binding(),
        job_ref=f"plan-transport-tamper:{tamper_kind}",
    )
    with pytest.raises(
        IdeaSkillUnavailable,
        match="plan_primary_result_contract_invalid",
    ):
        adapter.generate_draft(request)
    operation = next(workspace.glob("provider-operations/*/primary"))
    if tamper_kind == "missing":
        (operation / "last-message.json").unlink()
    else:
        (operation / "last-message.json").write_text(
            "changed-after-seal",
            encoding="utf-8",
        )

    restarted = CodexPlanSkillAdapter(workspace, executable=str(executable))
    with pytest.raises(
        IdeaSkillUnavailable,
        match="codex_operation_spool_invalid",
    ) as rejected:
        restarted.generate_draft(
            replace(request, runtime_binding=restarted.runtime_binding())
        )
    assert rejected.value.recovery_checkpoint is None


def test_plan_production_adapter_missing_review_trace_never_replays(
    tmp_path: Path,
) -> None:
    data_path = tmp_path / "plan-production-review-trace"
    workspace = tmp_path / "plan-production-provider"
    outputs: list[dict[str, object]] = []
    runner = _SequenceRunner(
        outputs,
        emit_review_spawn=False,
        emit_review_wait=False,
    )
    adapter = CodexPlanSkillAdapter(workspace, process_runner=runner)
    runtime = _plan_runtime(
        data_path,
        idea_skill=_DeterministicIdeaSkill(),
        plan_skill=adapter,  # type: ignore[arg-type]
    )
    try:
        quest = _confirm_direct_quest(runtime)
        _finish_idea_stage(runtime)
        for _step in range(4):
            current = runtime.plan_stage.query_current()
            if current["run"] is not None:
                break
            assert runtime.plan_stage.process_once()
        request = runtime.owners.advancement_engine.query_plan_stage_request(
            quest["cycle_ref"]
        )
        assert request is not None
        run = runtime.owners.agent_runtime.query_plan_stage_run(request.request_ref)
        assert run is not None
        accepted_content = runtime.plan_stage._accepted_question_content(  # type: ignore[attr-defined]
            request.accepted_question
        )
        assert request.accepted_idea_set is not None
        skill_request = PlanSkillRequest(
            stage_request_ref=request.request_ref,
            cycle_ref=request.cycle_ref,
            question_ref=request.accepted_question.question_ref,
            idea_set_ref=request.accepted_idea_set.outcome_ref,
            context_pack_ref=request.context_pack_ref,
            context_pack_hash=request.context_pack_hash,
            context_pack=request.context_pack,
            accepted_question_content=accepted_content,
            accepted_idea_set=request.accepted_idea_set.idea_set,
            root_session_ref=run.root_session_ref,
            submission_revision=run.attempt_generation,
            runtime_binding=run.runtime_binding,
            job_ref=run.primary_invocation.operation_ref,
        )
        document = _DeterministicPlanSkill(no_gap=False)._document(skill_request)
        outputs.extend(
            [
                {"plan": document},
                {
                    "reviewer_agent_ref": "plan-reviewer:missing-trace",
                    "findings": [],
                    "final_plan": document,
                    "dispositions": [],
                },
            ]
        )

        assert runtime.plan_stage.process_once()
        assert not runtime.plan_stage.process_once()
        blocked_view = runtime.plan_stage.query_current()
        blocked = blocked_view["run"]
        assert blocked["status"] == "suspended_fenced"
        assert blocked["fence_status"] == "revoked"
        assert blocked["blocker"] == {
            "status": "durable",
            "reason": {"code": "codex_child_review_spawn_invalid"},
        }
        checkpoint = blocked["recovery_checkpoint"]
        provider_exit = checkpoint["checkpoint"]["provider_exit"]
        assert provider_exit["schema_ref"] == (
            "meta-research/provider-terminal-contract-failure/v1"
        )
        assert provider_exit["contract_failure_code"] == (
            "codex_child_review_spawn_invalid"
        )
        assert provider_exit["contract_failure_detail_code"] == (
            "codex_child_review_spawn_invalid"
        )
        assert checkpoint["checkpoint"]["attempt_ref"] == blocked["attempt_ref"]
        assert checkpoint["checkpoint"]["fence_ref"] == blocked["fence_ref"]
        assert blocked_view["stage_commit"] is None
        operation_ref = run.review_invocation.operation_ref
        terminal_rows = _provider_terminal_rows(runtime, operation_ref)
        assert len(terminal_rows["units"]) == 1
        assert terminal_rows["units"][0][1] == "revoked"
        assert terminal_rows["units"][0][3] is not None
        assert len(terminal_rows["responsibilities"]) == 1
        assert terminal_rows["responsibilities"][0][1:3] == (
            "finished",
            "permanent_fence",
        )
        assert len(runner.calls) == 2

        assert not runtime.plan_stage.process_once()
        assert runtime.plan_stage.query_current()["stage_commit"] is None
        assert _provider_terminal_rows(runtime, operation_ref) == terminal_rows
        assert len(runner.calls) == 2
    finally:
        runtime.close()

    no_replay = _SequenceRunner([])
    restarted_adapter = CodexPlanSkillAdapter(
        workspace,
        process_runner=no_replay,
    )
    restarted = _plan_runtime(
        data_path,
        idea_skill=_DeterministicIdeaSkill(),
        plan_skill=restarted_adapter,  # type: ignore[arg-type]
    )
    try:
        assert not restarted.plan_stage.process_once()
        restarted_view = restarted.plan_stage.query_current()
        assert restarted_view["run"]["status"] == "suspended_fenced"
        assert restarted_view["run"]["fence_status"] == "revoked"
        assert restarted_view["stage_commit"] is None
        assert _provider_terminal_rows(restarted, operation_ref) == terminal_rows
        assert no_replay.calls == []
    finally:
        restarted.close()


def test_bundle_production_adapter_missing_review_trace_never_replays(
    tmp_path: Path,
) -> None:
    data_path = tmp_path / "bundle-production-review-trace"
    workspace = tmp_path / "bundle-production-provider"
    outputs: list[dict[str, object]] = []
    runner = _ResidentSequenceRunner(
        outputs,
        emit_review_spawn=False,
        emit_review_wait=False,
    )
    adapter = CodexBundleSkillAdapter(workspace, process_runner=runner)
    runtime = _bundle_runtime(
        data_path,
        bundle_skill_provider=adapter,
    )
    runtime.bundle_stage.configure_resident_mcp_endpoint("http://127.0.0.1:8765")
    try:
        _prepare_bundle_request(runtime)
        for _step in range(3):
            current = runtime.bundle_stage.query_current()
            if current["run"] is not None:
                break
            assert runtime.bundle_stage.process_once()
        current = runtime.bundle_stage.query_current()
        request_view = current["stage_run_request"]
        assert request_view is not None
        request = runtime.owners.advancement_engine.query_bundle_stage_request(
            request_view["cycle_ref"]
        )
        assert request is not None and request.accepted_formal_plan is not None
        run = runtime.owners.agent_runtime.query_bundle_stage_run(request.request_ref)
        assert run is not None
        skill_request = BundleSkillRequest(
            stage_request_ref=request.request_ref,
            run_ref=run.run_ref,
            attempt_ref=run.attempt_ref,
            fence_ref=run.fence_ref,
            cycle_ref=request.cycle_ref,
            question_ref=request.accepted_question.question_ref,
            formal_plan_ref=request.accepted_formal_plan.formal_plan_ref,
            context_pack_ref=request.context_pack_ref,
            context_pack_hash=request.context_pack_hash,
            context_pack=request.context_pack,
            plan_document=request.accepted_formal_plan.plan_document,
            root_session_ref=run.root_session_ref,
            runtime_binding=run.runtime_binding,
            inbox_checkpoint={},
            job_ref=run.primary_invocation.operation_ref,
        )
        target_plan = _DeterministicBundleSkill()._target_plan(skill_request)
        outputs.extend(
            [
                {"target_plan": target_plan},
                {
                    "reviewer_agent_ref": "bundle-reviewer:missing-trace",
                    "findings": [],
                    "final_target_plan": target_plan,
                    "dispositions": [],
                },
            ]
        )

        assert runtime.bundle_stage.process_once()
        assert not runtime.bundle_stage.process_once()
        blocked = runtime.bundle_stage.query_current()["run"]
        assert blocked["status"] == "suspended_fenced"
        assert blocked["blocker"]["reason"] == {
            "code": "codex_child_review_spawn_invalid"
        }
        operation_ref = run.review_invocation.operation_ref
        terminal_rows = _provider_terminal_rows(runtime, operation_ref)
        assert len(runner.calls) == 2

        assert not runtime.bundle_stage.process_once()
        assert _provider_terminal_rows(runtime, operation_ref) == terminal_rows
        assert len(runner.calls) == 2
    finally:
        runtime.close()

    no_replay = _ResidentSequenceRunner([])
    restarted_adapter = CodexBundleSkillAdapter(
        workspace,
        process_runner=no_replay,
    )
    restarted = _bundle_runtime(
        data_path,
        bundle_skill_provider=restarted_adapter,
    )
    restarted.bundle_stage.configure_resident_mcp_endpoint(
        "http://127.0.0.1:8765"
    )
    try:
        assert not restarted.bundle_stage.process_once()
        assert _provider_terminal_rows(restarted, operation_ref) == terminal_rows
        assert no_replay.calls == []
    finally:
        restarted.close()


@pytest.mark.parametrize(
    ("rolling_operation", "detail_code", "expected_calls"),
    [
        ("dispatch", "codex_bundle_dispatch_invalid", 3),
        (
            "dispatch-contract",
            "bundle_dispatch_requires_authoritative_blocker",
            3,
        ),
        ("target-batch", "codex_bundle_target_batch_invalid", 4),
    ],
)
def test_bundle_rolling_invalid_result_is_terminal_without_replay(
    tmp_path: Path,
    rolling_operation: str,
    detail_code: str,
    expected_calls: int,
) -> None:
    data_path = tmp_path / f"bundle-rolling-{rolling_operation}-failure"
    workspace = tmp_path / f"bundle-rolling-{rolling_operation}-provider"
    runtime_holder: dict[str, object] = {}

    def provider_outputs():
        runtime = runtime_holder["runtime"]
        current = runtime.bundle_stage.query_current()
        request_view = current["stage_run_request"]
        request = runtime.owners.advancement_engine.query_bundle_stage_request(
            request_view["cycle_ref"]
        )
        run = runtime.owners.agent_runtime.query_bundle_stage_run(
            request.request_ref
        )
        assert request.accepted_formal_plan is not None and run is not None
        skill_request = BundleSkillRequest(
            stage_request_ref=request.request_ref,
            run_ref=run.run_ref,
            attempt_ref=run.attempt_ref,
            fence_ref=run.fence_ref,
            cycle_ref=request.cycle_ref,
            question_ref=request.accepted_question.question_ref,
            formal_plan_ref=request.accepted_formal_plan.formal_plan_ref,
            context_pack_ref=request.context_pack_ref,
            context_pack_hash=request.context_pack_hash,
            context_pack=request.context_pack,
            plan_document=request.accepted_formal_plan.plan_document,
            root_session_ref=run.root_session_ref,
            runtime_binding=run.runtime_binding,
            inbox_checkpoint={},
            job_ref=run.primary_invocation.operation_ref,
        )
        target_plan = _DeterministicBundleSkill()._target_plan(skill_request)
        yield {"target_plan": target_plan}
        yield {
            "reviewer_agent_ref": "bundle-reviewer:rolling-shape",
            "findings": [],
            "final_target_plan": target_plan,
            "dispositions": [],
        }
        if rolling_operation.startswith("dispatch"):
            if rolling_operation == "dispatch-contract":
                yield {
                    "action": "wait",
                    "selected_target_ref": None,
                    "rationale": "Wait despite an executable accepted frontier.",
                }
            else:
                yield {"action": "dispatch"}
            return
        graph = runtime.bundle_stage.query_current()["target_graph"]
        frontier = graph["frontier"]
        assert frontier
        yield {
            "action": "dispatch",
            "selected_target_ref": (
                frontier[-1]
                if isinstance(frontier[-1], str)
                else frontier[-1]["target_ref"]
            ),
            "rationale": "Dispatch the current accepted frontier target.",
        }
        yield {"strategy_update": {}}

    runner = _BundleSequenceRunner(provider_outputs())  # type: ignore[arg-type]
    adapter = CodexBundleSkillAdapter(workspace, process_runner=runner)
    runtime = _bundle_runtime(data_path, bundle_skill_provider=adapter)
    runtime_holder["runtime"] = runtime
    runtime.bundle_stage.configure_resident_mcp_endpoint("http://127.0.0.1:8765")
    try:
        if rolling_operation == "target-batch":
            _accept_real_target_root_commit(runtime)
        else:
            _confirm_direct_quest(runtime)
            _finish_idea_stage(runtime)
            _finish_plan_stage(runtime)

        for _step in range(24):
            changed = runtime.bundle_stage.process_once()
            current = runtime.bundle_stage.query_current()
            run_view = current.get("run")
            if run_view is not None and run_view["status"] == "suspended_fenced":
                assert not changed
                break
        else:
            raise AssertionError(
                f"Bundle {rolling_operation} did not reach the terminal fence"
            )

        assert current["run"]["blocker"]["reason"] == {
            "code": "bundle_review_result_contract_invalid"
        }
        provider_exit = current["run"]["recovery_checkpoint"]["checkpoint"][
            "provider_exit"
        ]
        assert provider_exit["contract_failure_code"] == (
            "bundle_review_result_contract_invalid"
        )
        assert provider_exit["contract_failure_detail_code"] == detail_code
        request_ref = current["stage_run_request"]["request_ref"]
        run = runtime.owners.agent_runtime.query_bundle_stage_run(request_ref)
        assert run is not None
        operation_ref = run.review_invocation.operation_ref
        terminal_rows = _provider_terminal_rows(runtime, operation_ref)
        assert any(row[1] == "revoked" for row in terminal_rows["units"])
        assert any(
            row[1:3] == ("finished", "permanent_fence")
            for row in terminal_rows["responsibilities"]
        )
        assert len(runner.calls) == expected_calls

        assert not runtime.bundle_stage.process_once()
        assert _provider_terminal_rows(runtime, operation_ref) == terminal_rows
        assert len(runner.calls) == expected_calls
    finally:
        runtime.close()

    no_replay = _BundleSequenceRunner([])
    restarted = _bundle_runtime(
        data_path,
        bundle_skill_provider=CodexBundleSkillAdapter(
            workspace,
            process_runner=no_replay,
        ),
    )
    restarted.bundle_stage.configure_resident_mcp_endpoint(
        "http://127.0.0.1:8765"
    )
    try:
        assert not restarted.bundle_stage.process_once()
        assert _provider_terminal_rows(restarted, operation_ref) == terminal_rows
        assert no_replay.calls == []
    finally:
        restarted.close()


def test_bundle_exhaustion_non_independent_review_is_terminal_without_replay(
    tmp_path: Path,
) -> None:
    data_path = tmp_path / "bundle-exhaustion-review-contract"
    workspace = tmp_path / "bundle-exhaustion-review-provider"
    runtime_holder: dict[str, object] = {}

    def provider_outputs():
        runtime = runtime_holder["runtime"]
        current = runtime.bundle_stage.query_current()
        request_view = current["stage_run_request"]
        request = runtime.owners.advancement_engine.query_bundle_stage_request(
            request_view["cycle_ref"]
        )
        run = runtime.owners.agent_runtime.query_bundle_stage_run(
            request.request_ref
        )
        assert request.accepted_formal_plan is not None and run is not None
        skill_request = BundleSkillRequest(
            stage_request_ref=request.request_ref,
            run_ref=run.run_ref,
            attempt_ref=run.attempt_ref,
            fence_ref=run.fence_ref,
            cycle_ref=request.cycle_ref,
            question_ref=request.accepted_question.question_ref,
            formal_plan_ref=request.accepted_formal_plan.formal_plan_ref,
            context_pack_ref=request.context_pack_ref,
            context_pack_hash=request.context_pack_hash,
            context_pack=request.context_pack,
            plan_document=request.accepted_formal_plan.plan_document,
            root_session_ref=run.root_session_ref,
            runtime_binding=run.runtime_binding,
            inbox_checkpoint={},
            job_ref=run.primary_invocation.operation_ref,
        )
        assessment = _ExhaustionBundleSkill()._assessment(skill_request)
        yield assessment
        yield bundle_exhaustion_review_response_document(
            reviewer_agent_ref="codex-bundle-primary:1",
            reviewed_assessment_hash=canonical_hash(assessment),
        )

    runner = _BundleSequenceRunner(provider_outputs())  # type: ignore[arg-type]
    adapter = CodexBundleSkillAdapter(workspace, process_runner=runner)
    runtime = _bundle_runtime(data_path, bundle_skill_provider=adapter)
    runtime_holder["runtime"] = runtime
    runtime.bundle_stage.configure_resident_mcp_endpoint("http://127.0.0.1:8765")
    try:
        _confirm_direct_quest(runtime)
        _finish_idea_stage(runtime)
        _finish_plan_stage(runtime)
        for _step in range(10):
            changed = runtime.bundle_stage.process_once()
            current = runtime.bundle_stage.query_current()
            if (
                current["run"] is not None
                and current["run"]["status"] == "suspended_fenced"
            ):
                assert not changed
                break
        else:
            raise AssertionError("Bundle exhaustion review did not terminalize")

        assert current["run"]["blocker"]["reason"] == {
            "code": "bundle_review_result_contract_invalid"
        }
        provider_exit = current["run"]["recovery_checkpoint"]["checkpoint"][
            "provider_exit"
        ]
        assert provider_exit["contract_failure_code"] == (
            "bundle_review_result_contract_invalid"
        )
        assert provider_exit["contract_failure_detail_code"] == (
            "bundle_exhaustion_review_not_independent"
        )
        request_ref = current["stage_run_request"]["request_ref"]
        run = runtime.owners.agent_runtime.query_bundle_stage_run(request_ref)
        assert run is not None
        operation_ref = run.review_invocation.operation_ref
        terminal_rows = _provider_terminal_rows(runtime, operation_ref)
        assert len(runner.calls) == 2

        assert not runtime.bundle_stage.process_once()
        assert _provider_terminal_rows(runtime, operation_ref) == terminal_rows
        assert len(runner.calls) == 2
    finally:
        runtime.close()

    no_replay = _BundleSequenceRunner([])
    restarted = _bundle_runtime(
        data_path,
        bundle_skill_provider=CodexBundleSkillAdapter(
            workspace,
            process_runner=no_replay,
        ),
    )
    restarted.bundle_stage.configure_resident_mcp_endpoint(
        "http://127.0.0.1:8765"
    )
    try:
        assert not restarted.bundle_stage.process_once()
        assert _provider_terminal_rows(restarted, operation_ref) == terminal_rows
        assert no_replay.calls == []
    finally:
        restarted.close()


def test_reasoning_production_adapter_missing_review_trace_never_replays(
    tmp_path: Path,
) -> None:
    data_path = tmp_path / "reasoning-production-review-trace"
    workspace = tmp_path / "reasoning-production-provider"
    outputs: list[dict[str, object]] = []
    runner = _ResidentSequenceRunner(
        outputs,
        emit_review_spawn=False,
        emit_review_wait=False,
    )
    adapter = CodexReasoningSkillAdapter(workspace, process_runner=runner)
    runtime = _reasoning_runtime(
        data_path,
        reasoning_skill=adapter,  # type: ignore[arg-type]
    )
    runtime.reasoning_stage.configure_resident_mcp_endpoint(
        "http://127.0.0.1:8765"
    )
    try:
        quest = _confirm_deepfetch_quest(runtime)
        _finish_idea_stage(runtime)
        for _step in range(4):
            current = runtime.reasoning_stage.query_current()
            if current["run"] is not None:
                break
            assert runtime.reasoning_stage.process_once()
        request = runtime.owners.advancement_engine.query_reasoning_stage_request(
            quest["cycle_ref"]
        )
        assert request is not None
        run = runtime.owners.agent_runtime.query_reasoning_stage_run(
            request.request_ref
        )
        assert run is not None
        skill_request = runtime.reasoning_stage._skill_request(  # type: ignore[attr-defined]
            request,
            run,
            job_ref=run.primary_invocation.operation_ref,
        )
        draft = _DeterministicReasoningSkill().generate_draft(skill_request).draft
        outputs.extend(
            [
                draft,
                {
                    "schema_ref": "meta-research/reasoning-review/v1",
                    "reviewer_agent_ref": "reasoning-reviewer:missing-trace",
                    "findings": [],
                    "final_output": draft,
                    "dispositions": [],
                },
            ]
        )

        assert runtime.reasoning_stage.process_once()
        assert not runtime.reasoning_stage.process_once()
        blocked = runtime.reasoning_stage.query_current()["run"]
        assert blocked["status"] == "suspended_fenced"
        assert blocked["blocker"]["reason"] == {
            "code": "codex_child_review_spawn_invalid"
        }
        operation_ref = run.review_invocation.operation_ref
        terminal_rows = _provider_terminal_rows(runtime, operation_ref)
        assert len(runner.calls) == 2

        assert not runtime.reasoning_stage.process_once()
        assert _provider_terminal_rows(runtime, operation_ref) == terminal_rows
        assert len(runner.calls) == 2
    finally:
        runtime.close()

    no_replay = _ResidentSequenceRunner([])
    restarted_adapter = CodexReasoningSkillAdapter(
        workspace,
        process_runner=no_replay,
    )
    restarted = _reasoning_runtime(
        data_path,
        reasoning_skill=restarted_adapter,  # type: ignore[arg-type]
    )
    restarted.reasoning_stage.configure_resident_mcp_endpoint(
        "http://127.0.0.1:8765"
    )
    try:
        assert not restarted.reasoning_stage.process_once()
        assert _provider_terminal_rows(restarted, operation_ref) == terminal_rows
        assert no_replay.calls == []
    finally:
        restarted.close()


@pytest.mark.parametrize(
    ("phase", "failure_code", "missing_semantic_call", "expected_calls"),
    [
        ("primary", "reasoning_primary_result_contract_invalid", 0, 1),
        ("review", "reasoning_review_result_contract_invalid", 1, 2),
    ],
)
def test_reasoning_missing_semantic_trace_is_terminal_without_replay(
    tmp_path: Path,
    phase: str,
    failure_code: str,
    missing_semantic_call: int,
    expected_calls: int,
) -> None:
    data_path = tmp_path / f"reasoning-{phase}-semantic-trace"
    workspace = tmp_path / f"reasoning-{phase}-semantic-provider"
    outputs: list[dict[str, object]] = []
    runner = _SelectiveSemanticSequenceRunner(
        outputs,
        missing_semantic_call=missing_semantic_call,
    )
    adapter = CodexReasoningSkillAdapter(workspace, process_runner=runner)
    runtime = _reasoning_runtime(
        data_path,
        reasoning_skill=adapter,  # type: ignore[arg-type]
    )
    runtime.reasoning_stage.configure_resident_mcp_endpoint(
        "http://127.0.0.1:8765"
    )
    try:
        quest = _confirm_deepfetch_quest(runtime)
        _finish_idea_stage(runtime)
        for _step in range(4):
            current = runtime.reasoning_stage.query_current()
            if current["run"] is not None:
                break
            assert runtime.reasoning_stage.process_once()
        request = runtime.owners.advancement_engine.query_reasoning_stage_request(
            quest["cycle_ref"]
        )
        assert request is not None
        run = runtime.owners.agent_runtime.query_reasoning_stage_run(
            request.request_ref
        )
        assert run is not None
        skill_request = runtime.reasoning_stage._skill_request(  # type: ignore[attr-defined]
            request,
            run,
            job_ref=run.primary_invocation.operation_ref,
        )
        draft = _DeterministicReasoningSkill().generate_draft(skill_request).draft
        outputs.append(draft)
        if phase == "review":
            outputs.append(
                {
                    "schema_ref": "meta-research/reasoning-review/v1",
                    "reviewer_agent_ref": "reasoning-reviewer:semantic-trace",
                    "findings": [],
                    "final_output": draft,
                    "dispositions": [],
                }
            )
            assert runtime.reasoning_stage.process_once()

        assert not runtime.reasoning_stage.process_once()
        blocked = runtime.reasoning_stage.query_current()["run"]
        assert blocked["status"] == "suspended_fenced"
        assert blocked["blocker"]["reason"] == {"code": failure_code}
        provider_exit = blocked["recovery_checkpoint"]["checkpoint"][
            "provider_exit"
        ]
        assert provider_exit["contract_failure_code"] == failure_code
        assert provider_exit["contract_failure_detail_code"] == (
            "reasoning_semantic_mcp_currentness_unobserved"
        )
        operation_ref = (
            run.primary_invocation.operation_ref
            if phase == "primary"
            else run.review_invocation.operation_ref
        )
        terminal_rows = _provider_terminal_rows(runtime, operation_ref)
        assert len(runner.calls) == expected_calls

        assert not runtime.reasoning_stage.process_once()
        assert _provider_terminal_rows(runtime, operation_ref) == terminal_rows
        assert len(runner.calls) == expected_calls
    finally:
        runtime.close()

    no_replay = _ResidentSequenceRunner([])
    restarted = _reasoning_runtime(
        data_path,
        reasoning_skill=CodexReasoningSkillAdapter(
            workspace,
            process_runner=no_replay,
        ),  # type: ignore[arg-type]
    )
    restarted.reasoning_stage.configure_resident_mcp_endpoint(
        "http://127.0.0.1:8765"
    )
    try:
        assert not restarted.reasoning_stage.process_once()
        assert _provider_terminal_rows(restarted, operation_ref) == terminal_rows
        assert no_replay.calls == []
    finally:
        restarted.close()


@pytest.mark.parametrize(
    "termination_reason",
    ["descendant_process", "launch_failed"],
)
def test_idea_other_signed_terminal_exits_are_fenced_without_replay(
    tmp_path: Path,
    termination_reason: str,
) -> None:
    provider = _TerminalFailureIdeaProvider(termination_reason)
    runtime = _runtime(
        prepare_data_root(tmp_path / termination_reason), provider
    )
    try:
        _confirm_question(runtime)
        runtime.idea_stage.start(f"idea-{termination_reason}-start")

        assert not runtime.idea_stage.process_once()
        run = runtime.idea_stage.query_current()["run"]
        assert run["status"] == "suspended_fenced"
        assert run["blocker"] == {
            "status": "durable",
            "reason": {"code": "codex_operation_failed"},
        }
        assert run["recovery_checkpoint"]["checkpoint"]["provider_exit"] == (
            _signed_ceiling_evidence(termination_reason)
        )

        assert not runtime.idea_stage.process_once()
        assert provider.generate_calls == 1
    finally:
        runtime.close()


def test_plan_signed_output_ceiling_is_a_durable_nonterminal_blocker(
    tmp_path: Path,
) -> None:
    provider = _CeilingPlanProvider()
    runtime = _plan_runtime(
        tmp_path / "plan-hard-ceiling",
        idea_skill=_DeterministicIdeaSkill(),
        plan_skill=provider,
    )
    try:
        _confirm_direct_quest(runtime)
        _finish_idea_stage(runtime)
        for _step in range(4):
            changed = runtime.plan_stage.process_once()
            if provider.generate_calls:
                assert not changed
                break
            assert changed
        else:
            raise AssertionError("Plan provider boundary was not reached")

        current = runtime.plan_stage.query_current()
        run = current["run"]
        assert run["status"] == "suspended_fenced"
        assert run["blocker"]["reason"] == {
            "code": "codex_operation_output_limit"
        }
        assert run["recovery_checkpoint"]["checkpoint"]["provider_exit"] == (
            _signed_ceiling_evidence("output_limit")
        )
        assert current["stage_commit"] is None
        assert not runtime.plan_stage.process_once()
        assert provider.generate_calls == 1
    finally:
        runtime.close()


def test_bundle_signed_timeout_fences_current_attempt_without_blind_replay(
    tmp_path: Path,
) -> None:
    provider = _CeilingBundleProvider()
    runtime = _bundle_runtime(
        tmp_path / "bundle-hard-ceiling",
        bundle_skill_provider=provider,
    )
    try:
        _prepare_bundle_request(runtime)
        for _step in range(4):
            changed = runtime.bundle_stage.process_once()
            if provider.generate_calls:
                assert not changed
                break
            assert changed
        else:
            raise AssertionError("Bundle provider boundary was not reached")

        current = runtime.bundle_stage.query_current()
        run = current["run"]
        assert run["status"] == "suspended_fenced"
        assert run["fence_status"] == "revoked"
        assert run["blocker"]["reason"] == {"code": "codex_operation_timeout"}
        assert run["recovery_checkpoint"]["checkpoint"]["provider_exit"] == (
            _signed_ceiling_evidence("timeout")
        )
        assert current["stage_commit"] is None
        assert not runtime.bundle_stage.process_once()
        assert provider.generate_calls == 1
    finally:
        runtime.close()


def test_resume_after_production_ceiling_uses_a_new_physical_operation(
    tmp_path: Path,
) -> None:
    invocation_count = tmp_path / "provider-invocation-count"
    executable = tmp_path / "codex"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        "from pathlib import Path\n"
        "import sys\n"
        "import time\n"
        "if '--version' in sys.argv:\n"
        "    print('codex-stage-ceiling-test 1')\n"
        "    raise SystemExit(0)\n"
        "prompt = sys.stdin.buffer.read().decode('utf-8')\n"
        f"counter = Path({str(invocation_count)!r})\n"
        "count = int(counter.read_text()) + 1 if counter.exists() else 1\n"
        "counter.write_text(str(count))\n"
        "if count == 1:\n"
        "    time.sleep(2)\n"
        "def value(prefix):\n"
        "    return next(line.split('=', 1)[1] for line in prompt.splitlines() "
        "if line.startswith(prefix))\n"
        "question_ref = value('question_ref=')\n"
        "context_pack_ref = value('context_pack_ref=')\n"
        "outcome = {\n"
        "    'kind': 'IdeaSet',\n"
        "    'question_ref': question_ref,\n"
        "    'context_pack_ref': context_pack_ref,\n"
        "    'candidates': [{\n"
        "        'candidate_key': 'resumed-operation',\n"
        "        'direction': '以跨增强拓扑一致性约束自监督去噪。',\n"
        "        'rationale': '新物理操作可从永久封存的旧 ceiling 后继续。',\n"
        "        'assumptions': ['受控增强不改变稀有形态拓扑。'],\n"
        "        'risks': ['增强可能保留传感器伪影。'],\n"
        "        'evidence_boundary': {\n"
        "            'accepted_evidence_refs': [],\n"
        "            'supported': 'Question 固定了低照度形态保真范围。',\n"
        "            'inferred': '拓扑一致性可能提高稀有结构保真。',\n"
        "            'unknown': '跨设备稳健性仍未知。',\n"
        "        },\n"
        "        'falsification_hint': {\n"
        "            'test': '比较稀有形态召回率与伪影率。',\n"
        "            'would_refute': '召回率未提高或伪影显著增加。',\n"
        "        },\n"
        "        'material_difference': {\n"
        "            'from_history': '不复用旧 ceiling 输出。',\n"
        "            'from_peers': '以拓扑而非像素误差组织机制。',\n"
        "            'plan_commitment_change': 'Plan 比较拓扑干预轴与基线。',\n"
        "        },\n"
        "    }],\n"
        "    'recommendation': None,\n"
        "}\n"
        "args = sys.argv[1:]\n"
        "result_path = Path(args[args.index('--output-last-message') + 1])\n"
        "result_path.write_text(json.dumps({'outcome': outcome}), encoding='utf-8')\n"
        "print(json.dumps({'type': 'thread.started', "
        "'thread_id': 'resumed-primary'}))\n",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    adapter = CodexIdeaSkillAdapter(
        tmp_path / "idea-provider",
        executable=str(executable),
        timeout_seconds=0.2,
    )
    drafting = _DraftingProvider()
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "production-ceiling-resume"),
        proposal_drafter=drafting,
        intent_drafting_provider=drafting,
        host_compute_probe=_ComputeProbe(),
        idea_skill_provider=adapter,
    )
    try:
        completed = _confirm_question(runtime)
        runtime.idea_stage.start("production-ceiling-start")
        assert not runtime.idea_stage.process_once()
        fenced = runtime.idea_stage.query_current()["run"]
        assert fenced["status"] == "suspended_fenced"
        assert invocation_count.read_text(encoding="utf-8") == "1"

        foreground = runtime.owners.advancement_engine.query_foreground(
            completed["quest_ref"]
        )
        assert foreground is not None
        resume = _confirmed_control(
            runtime.owners.human_collaboration,
            scope_ref=f"quest:{completed['quest_ref']}",
            payload={
                "action": "resume",
                "target": {
                    "quest_ref": completed["quest_ref"],
                    "cycle_ref": completed["cycle_ref"],
                    "question_ref": foreground["question_ref"],
                    "epoch": foreground["epoch"],
                    "target_scope": "run",
                    "run_ref": fenced["run_ref"],
                },
                "reason": "operator_requested",
            },
            key="production-ceiling-resume",
        )
        _execute_control(
            runtime.owners.human_collaboration,
            resume,
            "production-ceiling-resume",
        )
        resumed = runtime.idea_stage.query_current()["run"]
        assert resumed["attempt_ref"] != fenced["attempt_ref"]
        assert resumed["fence_ref"] != fenced["fence_ref"]
        assert resumed["root_session_ref"] == fenced["root_session_ref"]
        assert (
            resumed["provider_operations"]["primary"]["invocation_ref"]
            != fenced["provider_operations"]["primary"]["invocation_ref"]
        )

        assert runtime.idea_stage.process_once()
        advanced = runtime.idea_stage.query_current()["run"]
        assert invocation_count.read_text(encoding="utf-8") == "2"
        assert advanced["primary_draft_checkpoint"]["status"] == "recorded"
        assert advanced["status"] == "admitted"
        operation_roots = sorted(
            path
            for path in (
                tmp_path / "idea-provider" / "provider-operations"
            ).iterdir()
            if path.is_dir()
        )
        assert len(operation_roots) == 2
        receipts = [
            (root / "primary" / "supervisor-exit.json").read_text(
                encoding="utf-8"
            )
            for root in operation_roots
        ]
        assert sum(
            '"termination_reason":"timeout"' in receipt for receipt in receipts
        ) == 1
        assert sum(
            '"termination_reason":"completed"' in receipt for receipt in receipts
        ) == 1
    finally:
        runtime.close()


def test_failed_inhibitor_prevents_runtime_version_probe_and_provider_spawn(
    tmp_path: Path,
) -> None:
    invocation_log = tmp_path / "codex-invocations.log"
    executable = tmp_path / "codex"
    executable.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$*\" >> {str(invocation_log)!r}\n"
        "printf 'codex-test 1.0.0\\n'\n",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    adapter = CodexIdeaSkillAdapter(
        tmp_path / "idea-provider",
        executable=str(executable),
    )
    drafting = _DraftingProvider()
    inhibitor = _UnavailableInhibitor()
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "pre-hold-probe"),
        proposal_drafter=drafting,
        intent_drafting_provider=drafting,
        host_compute_probe=_ComputeProbe(),
        idea_skill_provider=adapter,
        power_inhibitor=inhibitor,
    )
    try:
        _confirm_question(runtime)
        inhibitor.available = False
        runtime.idea_stage.start("pre-hold-probe-start")
        assert not runtime.idea_stage.process_once()

        assert not invocation_log.exists()
        assert runtime.idea_stage.transient_error == (
            "power_inhibitor_acquisition_failed"
        )
        waiting = runtime.query_runtime_observability()["durable_waiting"]
        assert len(waiting) == 1
        assert waiting[0]["reason"] == {
            "code": "power_inhibitor_acquisition_failed"
        }
    finally:
        runtime.close()
