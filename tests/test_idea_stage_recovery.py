from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from meta_research.composition import build_production_runtime
from meta_research.idea_skill import (
    CodexIdeaSkillAdapter,
    IdeaSkillDraft,
    IdeaSkillRequest,
    IdeaSkillResult,
    IdeaSkillUnavailable,
)
from meta_research.owners.agent_runtime import IdeaRuntimeBinding
from meta_research.owners.common import OwnerConflict, canonical_hash
from meta_research.paths import DataRoot, prepare_data_root
from meta_research.quest_drafting import (
    HostComputeDevice,
    HostComputeSnapshot,
    IntentTurnRequest,
    IntentTurnResult,
    ProposalDraftRequest,
    ProposalDraftResult,
)
from meta_research.web import create_app
from meta_research.semantic_mcp import ROOT_AGENT_HUMAN_REQUEST_OPERATION_IDS
from test_harness_full_conformance import (
    _FullConformanceAdapter,
    _full_request,
)


_QUESTION = {
    "title": "低照度显微图像中的稀有形态保真",
    "unknown_statement": "尚不明确哪种自监督条件能保留稀有形态。",
    "answer_shape": "形成带反例和证据边界的比较结论。",
    "applicability_scope": "低照度荧光显微公开数据。",
    "background_context": "研究稀有细胞形态。",
    "requirements_constraints": "两周内，使用获准 GPU。",
}


class _DraftingProvider:
    def draft(self, request: ProposalDraftRequest) -> ProposalDraftResult:
        return ProposalDraftResult(_QUESTION, "test_deterministic")

    def reply(self, request: IntentTurnRequest) -> IntentTurnResult:
        return IntentTurnResult(
            "测试回复",
            request.native_session_ref or "intent-native-session",
            "test_deterministic",
        )


class _ComputeProbe:
    def observe(self) -> HostComputeSnapshot:
        return HostComputeSnapshot(
            status="ready",
            observed_at=1720000000.0,
            devices=(HostComputeDevice("GPU-test-1", "Test GPU", 81920),),
            adapter_kind="test_probe",
        )


class _IdeaProvider:
    def __init__(self, *, reject_first: bool = False) -> None:
        self.reject_first = reject_first
        self.requests: list[IdeaSkillRequest] = []

    def runtime_binding(self) -> IdeaRuntimeBinding:
        return IdeaRuntimeBinding(
            packaged_skill_bundle_hash=canonical_hash({"skill": "test-idea"}),
            instruction_set_hash=canonical_hash({"instructions": "test-idea"}),
            model_ref="test-model-v1",
            harness_adapter_ref="test-deterministic-v1",
            mcp_bindings=(),
            capability_bindings=(),
            resource_bindings=(),
        )

    def generate_draft(self, request: IdeaSkillRequest) -> IdeaSkillDraft:
        self.requests.append(request)
        rejected_shape = self.reject_first and request.submission_revision == 1
        direction = (
            str(request.accepted_question_content["title"])
            if rejected_shape
            else "以跨增强的拓扑一致性约束自监督去噪。"
        )
        outcome = {
            "kind": "IdeaSet",
            "question_ref": request.question_ref,
            "context_pack_ref": request.context_pack_ref,
            "candidates": [
                {
                    "candidate_key": (
                        "rejected-title-copy"
                        if rejected_shape
                        else f"topology-consistency-r{request.submission_revision}"
                    ),
                    "direction": direction,
                    "rationale": (
                        "像素重建易压低低频形态，拓扑约束保留结构。"
                    ),
                    "assumptions": ["受控增强下稀有形态拓扑稳定。"],
                    "risks": ["约束可能同时保留传感器伪影。"],
                    "evidence_boundary": {
                        "accepted_evidence_refs": [],
                        "supported": (
                            "Question 固定了低照度形态保真的范围。"
                        ),
                        "inferred": "拓扑一致性可能提高稀有结构保真。",
                        "unknown": "跨设备稳健性仍未知。",
                    },
                    "falsification_hint": {
                        "test": "比较稀有形态召回率与伪影率。",
                        "would_refute": "召回率未提高或伪影显著增加。",
                    },
                    "material_difference": {
                        "from_history": "不复用已接纳 Idea。",
                        "from_peers": "以拓扑而非像素误差组织机制。",
                        "plan_commitment_change": (
                            "Plan 比较拓扑干预轴与基线。"
                        ),
                    },
                }
            ],
            "recommendation": None,
        }
        return IdeaSkillDraft(
            draft=outcome,
            primary_session_ref=(
                request.native_session_ref
                or f"codex-primary-{request.stage_request_ref}"
            ),
            adapter_kind="test_deterministic",
        )

    def review_draft(
        self, request: IdeaSkillRequest, draft: IdeaSkillDraft
    ) -> IdeaSkillResult:
        return IdeaSkillResult(
            reviewed_draft=draft.draft,
            final_outcome=draft.draft,
            findings=(),
            dispositions=(),
            primary_session_ref=draft.primary_session_ref,
            review_mode="advisory_unobserved",
            reviewer_agent_ref=None,
            adapter_kind=draft.adapter_kind,
        )

    def execute(self, request: IdeaSkillRequest) -> IdeaSkillResult:
        draft = self.generate_draft(request)
        return self.review_draft(request, draft)


class _RecoveringIdeaProvider(_IdeaProvider):
    def __init__(self) -> None:
        super().__init__()
        self.available = False

    def generate_draft(self, request: IdeaSkillRequest) -> IdeaSkillDraft:
        if not self.available:
            raise IdeaSkillUnavailable("codex_cli_unavailable")
        return super().generate_draft(request)


class _ReviewRecoveringIdeaProvider(_IdeaProvider):
    def __init__(self) -> None:
        super().__init__()
        self.review_calls = 0

    def review_draft(
        self, request: IdeaSkillRequest, draft: IdeaSkillDraft
    ) -> IdeaSkillResult:
        self.review_calls += 1
        if self.review_calls == 1:
            raise IdeaSkillUnavailable("codex_review_timeout")
        return super().review_draft(request, draft)


class _HumanRequestIdeaProvider(_IdeaProvider):
    def __init__(
        self,
        *,
        invalid_first: bool = False,
        phase: str = "primary",
    ) -> None:
        super().__init__()
        self.invalid_first = invalid_first
        self.phase = phase
        self.owner = None
        self.human_request: dict[str, object] | None = None
        self.review_requests: list[IdeaSkillRequest] = []
        self.finished_jobs: list[str] = []

    def generate_draft(self, request: IdeaSkillRequest) -> IdeaSkillDraft:
        draft = super().generate_draft(request)
        if self.phase != "primary" or self.human_request is not None:
            return draft
        self._open_human_request(request, phase="primary")
        if self.invalid_first:
            return IdeaSkillDraft(
                draft={**draft.draft, "question_ref": "question:wrong"},
                primary_session_ref=draft.primary_session_ref,
                adapter_kind=draft.adapter_kind,
            )
        return draft

    def review_draft(
        self, request: IdeaSkillRequest, draft: IdeaSkillDraft
    ) -> IdeaSkillResult:
        self.review_requests.append(request)
        result = super().review_draft(request, draft)
        if self.phase == "review" and self.human_request is None:
            self._open_human_request(request, phase="review")
        return result

    def finish_job(self, job_ref: str) -> None:
        self.finished_jobs.append(job_ref)

    def _open_human_request(
        self, request: IdeaSkillRequest, *, phase: str
    ) -> None:
        assert self.owner is not None
        managed = self.owner.query_managed_run(request.run_ref)
        assert managed is not None
        generation = request.submission_revision
        binding = {
            "quest_ref": managed["quest_ref"],
            "task_ref": request.run_ref,
            "root_session_ref": request.root_session_ref,
            "operation_id": ROOT_AGENT_HUMAN_REQUEST_OPERATION_IDS[0],
            "attempt_ref": request.attempt_ref,
            "generation": generation,
            "request_owner": "agent_runtime",
            "root_kind": "idea",
            "phase": phase,
            "fence_ref": request.fence_ref,
            "runtime_binding_hash": canonical_hash(
                request.runtime_binding.as_dict()
            ),
        }
        target = {
            "schema_ref": "meta-research/root-agent-human-request-target/v1",
            "root": {
                "run_kind": "idea",
                "run_ref": request.run_ref,
                "attempt_ref": request.attempt_ref,
                "root_session_ref": request.root_session_ref,
                "fence_ref": request.fence_ref,
                "waiter_generation": generation,
            },
            "condition": {"operator_choice": "continue_without_optional_input"},
        }
        self.human_request = self.owner.open_human_request_effect(
            effect_key=f"mcp-effect:idea-provider-human-wait:{phase}",
            effect_id=f"idea-provider-human-wait-{phase}",
            operation_binding=binding,
            predecessor_request_ref=None,
            request_kind="offline_action",
            obligation="Choose whether this exact Idea task should continue.",
            business_purpose="Resume only this exact Idea task.",
            target_assertion=target,
            acceptance_conditions=("The operator supplies a disposition.",),
            direct_waiter={
                "waiter_ref": f"root_run:{request.run_ref}",
                "generation": generation,
                "target_assertion": target,
                "wait_scope": "local",
                "other_blockers": [],
            },
            quest_ref=str(managed["quest_ref"]),
        )


class _NonzeroHumanRequestIdeaRunner:
    """Real Codex adapter seam using the authenticated resident MCP channel."""

    native_session_ref = "codex-idea-human-request-session"
    effect_id = "idea-provider-human-request-nonzero"

    def __init__(self) -> None:
        self.runtime = None
        self.provider_calls: list[tuple[list[str], str, dict[str, str]]] = []
        self.request_ref: str | None = None
        self.resolution: dict[str, object] | None = None

    def run_command(
        self, argv: list[str], timeout: float
    ) -> subprocess.CompletedProcess[str]:
        del timeout
        assert argv[-2:] == ["features", "list"]
        return subprocess.CompletedProcess(argv, 0, "", "")

    def __call__(
        self,
        argv: list[str],
        prompt: str,
        timeout: float | None,
        environment: dict[str, str],
    ) -> subprocess.CompletedProcess[str]:
        assert timeout is None or timeout > 0
        assert self.runtime is not None
        self.provider_calls.append((list(argv), prompt, dict(environment)))
        token = environment["META_RESEARCH_MCP_TOKEN"]
        if len(self.provider_calls) == 1:
            opened = self._tool_call(
                token,
                operation_id=ROOT_AGENT_HUMAN_REQUEST_OPERATION_IDS[0],
                arguments={
                    "effect_id": self.effect_id,
                    "request_kind": "offline_action",
                    "obligation": "Choose whether this Idea should continue.",
                    "business_purpose": "Resume this exact Idea root task.",
                    "condition": {
                        "impact": "Only this Idea task is paused.",
                        "safe_response": "Defer without providing secrets.",
                    },
                    "acceptance_conditions": [
                        "The response is bound to this exact request."
                    ],
                },
                request_id=1,
            )
            self.request_ref = str(opened["request_ref"])
        else:
            reconciled = self._tool_call(
                token,
                operation_id=ROOT_AGENT_HUMAN_REQUEST_OPERATION_IDS[1],
                arguments={"effect_id": self.effect_id},
                request_id=2,
            )
            resolution = reconciled.get("resolution")
            assert isinstance(resolution, dict)
            self.resolution = resolution
            question_ref = self._prompt_value(prompt, "question_ref")
            context_pack_ref = self._prompt_value(prompt, "context_pack_ref")
            output_path = Path(
                argv[argv.index("--output-last-message") + 1]
            )
            output_path.write_text(
                json.dumps(
                    {
                        "outcome": {
                            "kind": "IdeaSet",
                            "question_ref": question_ref,
                            "context_pack_ref": context_pack_ref,
                            "candidates": [
                                {
                                    "candidate_key": "human-response-continuation",
                                    "direction": "以跨增强拓扑一致性约束自监督去噪。",
                                    "rationale": "拓扑约束可能比像素重建更保留稀有结构。",
                                    "assumptions": [
                                        "受控增强下稀有形态拓扑稳定。"
                                    ],
                                    "risks": ["约束可能保留传感器伪影。"],
                                    "evidence_boundary": {
                                        "accepted_evidence_refs": [],
                                        "supported": "Question 限定了低照度形态保真。",
                                        "inferred": "拓扑一致性可能提高稀有结构保真。",
                                        "unknown": "跨设备稳健性仍未知。",
                                    },
                                    "falsification_hint": {
                                        "test": "比较稀有形态召回率与伪影率。",
                                        "would_refute": "召回未提高或伪影显著增加。",
                                    },
                                    "material_difference": {
                                        "from_history": "不复用已接纳 Idea。",
                                        "from_peers": "以拓扑而非像素误差组织机制。",
                                        "plan_commitment_change": "Plan 比较拓扑干预轴与基线。",
                                    },
                                }
                            ],
                            "recommendation": None,
                        }
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        stream = "\n".join(
            json.dumps(event)
            for event in (
                {
                    "type": "thread.started",
                    "thread_id": self.native_session_ref,
                },
                {
                    "type": "item.completed",
                    "item": {
                        "type": "mcp_tool_call",
                        "server": "meta_research",
                    },
                },
                {"type": "turn.completed"},
            )
        )
        first_call = len(self.provider_calls) == 1
        return subprocess.CompletedProcess(
            argv,
            17 if first_call else 0,
            stream + "\n",
            "provider exited after opening HumanRequest" if first_call else "",
        )

    def run_job(
        self,
        job_ref: str,
        argv: list[str],
        prompt: str,
        timeout: float | None,
        environment: dict[str, str],
    ) -> subprocess.CompletedProcess[str]:
        del job_ref
        return self(argv, prompt, timeout, environment)

    def _tool_call(
        self,
        token: str,
        *,
        operation_id: str,
        arguments: dict[str, object],
        request_id: int,
    ) -> dict[str, object]:
        status, payload, _session_id = self.runtime.harnesses.dispatch_mcp_http(
            token,
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "tools/call",
                "params": {"name": operation_id, "arguments": arguments},
            },
            mcp_session_id=self.native_session_ref,
        )
        assert status == 200
        assert payload is not None
        result = payload["result"]
        assert isinstance(result, dict) and result["isError"] is False
        structured = result["structuredContent"]
        assert isinstance(structured, dict)
        return structured

    @staticmethod
    def _prompt_value(prompt: str, field: str) -> str:
        prefix = f"{field}="
        value = next(
            line.removeprefix(prefix)
            for line in prompt.splitlines()
            if line.startswith(prefix)
        )
        assert value
        return value


class _PerRequestRecoveringIdeaProvider(_IdeaProvider):
    def __init__(self) -> None:
        super().__init__()
        self.blocked_request_ref: str | None = None

    def generate_draft(self, request: IdeaSkillRequest) -> IdeaSkillDraft:
        if request.stage_request_ref == self.blocked_request_ref:
            raise IdeaSkillUnavailable("request_specific_provider_unavailable")
        return super().generate_draft(request)


class _PerRequestRejectingIdeaProvider(_IdeaProvider):
    def __init__(self) -> None:
        super().__init__()
        self.rejected_request_ref: str | None = None

    def generate_draft(self, request: IdeaSkillRequest) -> IdeaSkillDraft:
        result = super().generate_draft(request)
        if request.stage_request_ref != self.rejected_request_ref:
            return result
        outcome = dict(result.draft)
        candidates = list(outcome["candidates"])
        candidate = dict(candidates[0])
        candidate["direction"] = str(request.accepted_question_content["title"])
        candidate["rationale"] = (
            "仍在复述 Question；纠错代次 "
            f"{request.submission_revision} 尚未形成新机制。"
        )
        candidates[0] = candidate
        outcome["candidates"] = candidates
        return IdeaSkillDraft(
            draft=outcome,
            primary_session_ref=result.primary_session_ref,
            adapter_kind=result.adapter_kind,
        )


def _runtime(data_root: DataRoot, provider: _IdeaProvider):
    drafting = _DraftingProvider()
    return build_production_runtime(
        data_root,
        proposal_drafter=drafting,
        intent_drafting_provider=drafting,
        host_compute_probe=_ComputeProbe(),
        idea_skill_provider=provider,
    )


def _confirm_question(runtime, suffix: str = "") -> dict[str, object]:
    def key(label: str) -> str:
        return f"recovery-{label}{('-' + suffix) if suffix else ''}"

    human = runtime.owners.human_collaboration
    opened = human.create_quest({}, key("open"))
    probed = human.observe_host_compute(
        opened["initialization_id"], ["GPU-test-1"], key("probe")
    )
    draft = dict(probed["quest_draft"]["value"])
    draft.update(
        {
            "goal": "判断低照度显微图像去噪能否保留稀有形态。",
            "completion_criteria": "形成带反例和证据边界的比较结论。",
            "time_budget": "30d",
            "route": "direct",
            "literature": {
                "mode": "oa_only",
                "library_entry_url": "",
                "scope_exclusions": "",
                "accepted_material_bindings": [],
            },
            "background_and_initial_direction": "比较自监督和监督基线。",
        }
    )
    saved = human.revise_quest_draft(
        opened["initialization_id"],
        draft,
        probed["quest_draft"]["hash"],
        key("draft"),
        probed["quest_draft"]["revision"],
    )
    human.generate_question_proposal(
        opened["initialization_id"],
        saved["quest_draft"]["hash"],
        key("proposal"),
        saved["quest_draft"]["revision"],
    )
    assert human.process_drafting_once()
    proposed = human.query_quest_creation(opened["initialization_id"])
    previewed = human.preview_confirmation(
        opened["initialization_id"],
        quest_draft_revision=proposed["quest_draft"]["revision"],
        quest_draft_hash=proposed["quest_draft"]["hash"],
        proposal_ref=proposed["proposal"]["ref"],
        proposal_hash=proposed["proposal"]["hash"],
        idempotency_key=key("preview"),
    )
    human.confirm_quest(
        opened["initialization_id"],
        quest_draft_revision=proposed["quest_draft"]["revision"],
        quest_draft_hash=proposed["quest_draft"]["hash"],
        proposal_ref=proposed["proposal"]["ref"],
        proposal_hash=proposed["proposal"]["hash"],
        preview_ref=previewed["confirmation_preview"]["ref"],
        preview_hash=previewed["confirmation_preview"]["hash"],
        idempotency_key=key("confirm"),
    )
    for _step in range(5):
        if not human.reconcile_once():
            break
    completed = human.query_quest_creation(opened["initialization_id"])
    assert completed["status"] == "completed"
    return completed


def test_restart_resumes_each_durable_boundary_without_provider_replay(
    tmp_path: Path,
) -> None:
    data_root = prepare_data_root(tmp_path / "restart-each-boundary")
    provider = _IdeaProvider()
    runtime = _runtime(data_root, provider)
    try:
        completed = _confirm_question(runtime)
        runtime.idea_stage.start("recovery-stage-start")
        for expected_status, expected_feed_delta in (
            ("not_attempted", 3),
            ("awaiting_content", 3),
            ("awaiting_domain", 1),
            ("accepted", 1),
            ("accepted", 1),
            ("accepted", 1),
        ):
            before = runtime.feed.current_revision()
            assert runtime.idea_stage.process_once()
            page = runtime.feed.read_after(before)
            core_events = tuple(
                event
                for event in page.events
                if not event.event_type.startswith(
                    (
                        "agent_runtime.power_inhibitor_",
                        "agent_runtime.runtime_",
                    )
                )
            )
            assert len(core_events) == expected_feed_delta
            assert runtime.idea_stage.query_current()["outcome_acceptance"][
                "status"
            ] == expected_status
            runtime.close()
            runtime = _runtime(data_root, provider)

        projection = runtime.projection.query_snapshot()
        assert set(projection["idea_stage"]) == {
            "eligibility",
            "stage_run_request",
            "run",
            "outcome_acceptance",
            "stage_commit",
        }
        assert projection["research_space"]["current_question"] == {
            "quest_ref": completed["quest_ref"],
            "question_ref": completed["question_ref"],
            "graph_revision": projection["owners"]["research_graph"]["revision"],
            "title": _QUESTION["title"],
            "unknown_statement": _QUESTION["unknown_statement"],
            "answer_shape": _QUESTION["answer_shape"],
            "applicability_scope": _QUESTION["applicability_scope"],
        }
        run = projection["idea_stage"]["run"]
        assert run["attempt_execution_receipt"]["kind"] == (
            "idea_attempt_execution"
        )
        assert run["attempt_execution_receipt"]["status"] == "accepted"
        assert run["completion_receipt"]["kind"] == "run_execution_completed"
        assert run["completion_receipt"]["status"] == "accepted"
        assert run["fence_status"] == "completed"
        assert projection["idea_stage"]["stage_commit"]["status"] == "Completed"
        assert len(provider.requests) == 1
        before = runtime.feed.current_revision()
        assert not runtime.idea_stage.process_once()
        assert runtime.feed.current_revision() == before

        app = create_app(
            runtime, base_url="http://testserver", control_key="control-secret"
        )
        with TestClient(app, base_url="http://testserver") as client:
            token = runtime.authentication.issue_bootstrap_token()
            authenticated = client.post(
                "/auth/bootstrap",
                headers={"Origin": "http://testserver"},
                json={"token": token},
            )
            assert authenticated.status_code == 200
            response = client.get("/api/v1/idea-stage/current")
            assert response.status_code == 200
            assert set(response.json()) == set(projection["idea_stage"])
    finally:
        runtime.close()


def test_restart_admits_run_after_request_response_gap(tmp_path: Path) -> None:
    data_root = prepare_data_root(tmp_path / "request-admission-gap")
    provider = _IdeaProvider()
    runtime = _runtime(data_root, provider)
    try:
        _confirm_question(runtime)

        def fail_admission(*_args, **_kwargs):
            raise OSError("simulated_admission_gap")

        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setattr(
                runtime.owners.agent_runtime,
                "admit_idea_stage",
                fail_admission,
            )
            with pytest.raises(OSError, match="simulated_admission_gap"):
                runtime.idea_stage.start("request-gap-start")
        requested = runtime.idea_stage.query_current()
        assert requested["stage_run_request"] is not None
        assert requested["run"] is None
    finally:
        runtime.close()

    runtime = _runtime(data_root, provider)
    try:
        before = runtime.feed.current_revision()
        assert runtime.idea_stage.process_once()
        assert runtime.feed.current_revision() == before + 1
        assert runtime.idea_stage.query_current()["run"]["status"] == "admitted"
        assert provider.requests == []
    finally:
        runtime.close()


def test_reviewer_failure_resumes_the_durably_bound_primary_session(
    tmp_path: Path,
) -> None:
    data_root = prepare_data_root(tmp_path / "primary-reviewer-gap")
    provider = _ReviewRecoveringIdeaProvider()
    runtime = _runtime(data_root, provider)
    try:
        completed = _confirm_question(runtime)
        runtime.idea_stage.start("primary-reviewer-gap-start")
        assert runtime.idea_stage.process_once()
        request = runtime.owners.advancement_engine.query_idea_stage_request(
            completed["cycle_ref"]
        )
        assert request is not None
        primary = runtime.owners.agent_runtime.query_idea_stage_run(
            request.request_ref
        )
        assert primary is not None and primary.primary_draft is not None
        native_session_ref = primary.native_session_ref
        assert native_session_ref == primary.primary_draft.native_session_ref
        assert primary.execution is None
        assert len(provider.requests) == 1
    finally:
        runtime.close()
    runtime = _runtime(data_root, provider)
    try:
        assert not runtime.idea_stage.process_once()
        after_timeout = runtime.owners.agent_runtime.query_idea_stage_run(
            request.request_ref
        )
        assert after_timeout is not None
        assert after_timeout.native_session_ref == native_session_ref
        assert after_timeout.primary_draft == primary.primary_draft
        assert after_timeout.execution is None
        assert len(provider.requests) == 1
        assert provider.review_calls == 1
    finally:
        runtime.close()

    runtime = _runtime(data_root, provider)
    try:
        assert runtime.idea_stage.process_once()
        recovered = runtime.owners.agent_runtime.query_idea_stage_run(
            request.request_ref
        )
        assert recovered is not None and recovered.execution is not None
        assert recovered.native_session_ref == native_session_ref
        assert recovered.execution.native_session_ref == native_session_ref
        assert len(provider.requests) == 1
        assert provider.review_calls == 2
        for _boundary in range(4):
            assert runtime.idea_stage.process_once()
        assert runtime.idea_stage.query_current()["stage_commit"]["status"] == (
            "Completed"
        )
    finally:
        runtime.close()


@pytest.mark.parametrize(
    "invalid_first",
    (False, True),
    ids=("valid-return", "invalid-candidate-return"),
)
def test_human_request_resumes_idea_in_same_native_session_with_new_job(
    tmp_path: Path,
    invalid_first: bool,
) -> None:
    data_root = prepare_data_root(tmp_path / "idea-human-request-continuation")
    provider = _HumanRequestIdeaProvider(invalid_first=invalid_first)
    runtime = _runtime(data_root, provider)
    provider.owner = runtime.owners.agent_runtime
    try:
        completed = _confirm_question(runtime, "human-request-continuation")
        runtime.idea_stage.start("human-request-continuation-start")

        assert runtime.idea_stage.process_once()
        request = runtime.owners.advancement_engine.query_idea_stage_request(
            completed["cycle_ref"]
        )
        assert request is not None
        parked = runtime.owners.agent_runtime.query_idea_stage_run(
            request.request_ref
        )
        assert parked is not None
        assert parked.primary_draft is None
        assert parked.native_session_ref is not None
        assert provider.human_request is not None
        assert provider.finished_jobs == [provider.requests[0].job_ref]
        assert runtime.owners.agent_runtime.query_managed_run(parked.run_ref)[
            "status"
        ] == "suspended"

        runtime.owners.human_collaboration.respond_to_human_request(
            str(provider.human_request["request_ref"]),
            decision="deferred",
            facts={},
            note="Continue without this optional input.",
            idempotency_key="idea-human-request-deferred",
        )
        assert runtime.owners.agent_runtime.query_managed_run(parked.run_ref)[
            "status"
        ] == "running"

        assert runtime.idea_stage.process_once()
        assert len(provider.requests) == 2
        first, resumed = provider.requests
        assert resumed.native_session_ref == parked.native_session_ref
        assert resumed.job_ref != first.job_ref
        assert str(resumed.job_ref).startswith("root_hr_continuation_")
        continued = runtime.owners.agent_runtime.query_idea_stage_run(
            request.request_ref
        )
        assert continued is not None and continued.primary_draft is not None
        assert continued.native_session_ref == parked.native_session_ref
    finally:
        runtime.close()


def test_real_idea_nonzero_after_human_request_resumes_same_session(
    tmp_path: Path,
) -> None:
    data_root = prepare_data_root(tmp_path / "real-idea-human-request-nonzero")
    drafting = _DraftingProvider()
    setup_runtime = build_production_runtime(
        data_root,
        proposal_drafter=drafting,
        intent_drafting_provider=drafting,
        host_compute_probe=_ComputeProbe(),
        idea_skill_provider=_IdeaProvider(),
        harness_adapters=(
            _FullConformanceAdapter("codex"),
            _FullConformanceAdapter("claude"),
        ),
        startup_power_probe=False,
        startup_harness_diagnostics=False,
    )
    setup_runtime.harnesses.start_full_conformance(_full_request())
    for _turn in range(4):
        if setup_runtime.harnesses.query_status()["status"] == "ready":
            break
        assert setup_runtime.harnesses.advance_full_conformance(
            mcp_base_url="http://127.0.0.1:8999"
        )
    assert setup_runtime.harnesses.query_status()["status"] == "ready"
    completed = _confirm_question(setup_runtime, "real-provider-nonzero")
    setup_runtime.close()

    runner = _NonzeroHumanRequestIdeaRunner()
    adapter = CodexIdeaSkillAdapter(
        data_root.run / "idea-human-request-provider",
        process_runner=runner,
    )
    runtime = build_production_runtime(
        data_root,
        proposal_drafter=drafting,
        intent_drafting_provider=drafting,
        host_compute_probe=_ComputeProbe(),
        idea_skill_provider=adapter,
        harness_adapters=(
            _FullConformanceAdapter("codex"),
            _FullConformanceAdapter("claude"),
        ),
        startup_power_probe=False,
        startup_harness_diagnostics=False,
    )
    runner.runtime = runtime
    runtime.configure_resident_mcp_endpoint("http://127.0.0.1:8999")
    try:
        runtime.idea_stage.start("real-idea-human-request-nonzero-start")

        assert runtime.idea_stage.process_once()
        request = runtime.owners.advancement_engine.query_idea_stage_request(
            completed["cycle_ref"]
        )
        assert request is not None
        parked = runtime.owners.agent_runtime.query_idea_stage_run(
            request.request_ref
        )
        assert parked is not None
        assert parked.primary_draft is None
        assert parked.native_session_ref == runner.native_session_ref
        assert runtime.owners.agent_runtime.query_managed_run(parked.run_ref)[
            "status"
        ] == "suspended"
        assert runner.request_ref is not None

        response = runtime.owners.human_collaboration.respond_to_human_request(
            runner.request_ref,
            decision="deferred",
            facts={},
            note="Continue without optional human input.",
            idempotency_key="real-idea-human-request-deferred",
        )
        assert runtime.idea_stage.process_once()

        continued = runtime.owners.agent_runtime.query_idea_stage_run(
            request.request_ref
        )
        assert continued is not None and continued.primary_draft is not None
        assert continued.native_session_ref == runner.native_session_ref
        assert len(runner.provider_calls) == 2
        first_argv = runner.provider_calls[0][0]
        resumed_argv = runner.provider_calls[1][0]
        resumed_prompt = runner.provider_calls[1][1]
        assert first_argv[-1] == "-"
        assert resumed_argv[-3:] == [
            "resume",
            runner.native_session_ref,
            "-",
        ]
        assert "human_request.open.reconcile" in resumed_prompt
        assert "agent_runtime.human_request.open.reconcile" not in resumed_prompt
        assert runner.resolution is not None
        assert runner.resolution["response_ref"] == response["response_ref"]
        assert runner.resolution["decision"] == "deferred"
        first_jobs = tuple(
            path.parent.parent.name
            for path in (
                data_root.run / "idea-human-request-provider" / "provider-operations"
            ).glob("*/primary/invocation.json")
        )
        assert len(first_jobs) == 2
    finally:
        runtime.close()


def test_human_request_park_finishes_the_current_idea_review_job(
    tmp_path: Path,
) -> None:
    provider = _HumanRequestIdeaProvider(phase="review")
    runtime = _runtime(
        prepare_data_root(tmp_path / "idea-review-human-request-finish"),
        provider,
    )
    provider.owner = runtime.owners.agent_runtime
    try:
        completed = _confirm_question(runtime, "idea-review-human-request-finish")
        runtime.idea_stage.start("idea-review-human-request-finish-start")

        assert runtime.idea_stage.process_once()
        request = runtime.owners.advancement_engine.query_idea_stage_request(
            completed["cycle_ref"]
        )
        assert request is not None
        checkpointed = runtime.owners.agent_runtime.query_idea_stage_run(
            request.request_ref
        )
        assert checkpointed is not None and checkpointed.primary_draft is not None

        assert runtime.idea_stage.process_once()
        parked = runtime.owners.agent_runtime.query_idea_stage_run(
            request.request_ref
        )
        assert parked is not None and parked.native_session_ref is not None
        assert runtime.owners.agent_runtime.query_managed_run(parked.run_ref)[
            "status"
        ] == "suspended"
        assert len(provider.review_requests) == 1
        assert provider.finished_jobs == [provider.review_requests[0].job_ref]
    finally:
        runtime.close()


def test_rejection_restart_reuses_native_session_and_rejects_old_fence(
    tmp_path: Path,
) -> None:
    data_root = prepare_data_root(tmp_path / "rejection-restart")
    first_provider = _IdeaProvider(reject_first=True)
    runtime = _runtime(data_root, first_provider)
    try:
        _confirm_question(runtime)
        runtime.idea_stage.start("rejection-stage-start")
        assert runtime.idea_stage.process_once()
        assert runtime.idea_stage.process_once()
        assert runtime.idea_stage.process_once()
        assert runtime.idea_stage.process_once()
        rejected = runtime.idea_stage.query_current()
        assert rejected["outcome_acceptance"]["status"] == "rejected"
        previous_run = runtime.owners.agent_runtime.query_idea_stage_run(
            rejected["stage_run_request"]["request_ref"]
        )
        assert previous_run is not None and previous_run.execution is not None
        native_session_ref = previous_run.native_session_ref
        assert native_session_ref is not None
        assert native_session_ref.startswith("codex-primary-")
    finally:
        runtime.close()

    revision_provider = _IdeaProvider()
    runtime = _runtime(data_root, revision_provider)
    try:
        # The first post-restart tick only closes the rejected fence and admits
        # its successor. It must not call the external provider.
        assert runtime.idea_stage.process_once()
        assert revision_provider.requests == []
        successor = runtime.owners.agent_runtime.query_idea_stage_run(
            previous_run.request_ref
        )
        assert successor is not None
        assert successor.attempt_generation == 2
        assert successor.native_session_ref == native_session_ref
        assert successor.predecessor_execution == previous_run.execution
        assert successor.rejection_receipt is not None

        with pytest.raises(OwnerConflict, match="attempt_fence_stale"):
            runtime.owners.agent_runtime.record_idea_attempt_execution(
                run_ref=previous_run.run_ref,
                attempt_ref=previous_run.attempt_ref,
                fence_ref=previous_run.fence_ref,
                submission_ref="stale_submission",
                native_session_ref=native_session_ref,
                runtime_binding=previous_run.runtime_binding,
                outcome=previous_run.execution.outcome,
                reviewed_draft=previous_run.execution.reviewed_draft,
                review=previous_run.execution.review,
                idempotency_key="stale-fence-must-fail",
            )

        runtime.close()
        runtime = _runtime(data_root, revision_provider)
        assert runtime.idea_stage.process_once()
        assert len(revision_provider.requests) == 1
        revision_request = revision_provider.requests[0]
        assert revision_request.native_session_ref == native_session_ref
        assert (
            revision_request.predecessor_submission_ref
            == previous_run.execution.submission_ref
        )
        assert revision_request.owner_rejection_receipt_ref
        assert revision_request.owner_feedback

        for _boundary in range(5):
            assert runtime.idea_stage.process_once()
        assert runtime.idea_stage.query_current()["stage_commit"]["status"] == (
            "Completed"
        )
    finally:
        runtime.close()


def test_worker_recovers_every_quest_without_latest_cycle_starvation(
    tmp_path: Path,
) -> None:
    data_root = prepare_data_root(tmp_path / "multiple-quest-recovery")
    provider = _PerRequestRecoveringIdeaProvider()
    runtime = _runtime(data_root, provider)
    try:
        first = _confirm_question(runtime, "first")
        runtime.idea_stage.start("first-stage-start")
        first_request = runtime.owners.advancement_engine.query_idea_stage_request(
            first["cycle_ref"]
        )
        assert first_request is not None
        provider.blocked_request_ref = first_request.request_ref
        assert not runtime.idea_stage.process_once()

        second = _confirm_question(runtime, "second")
        second_request = None
        for _boundary in range(12):
            runtime.idea_stage.process_once()
            second_request = (
                runtime.owners.advancement_engine.query_idea_stage_request(
                    second["cycle_ref"]
                )
            )
            if second_request is not None and (
                runtime.owners.advancement_engine.query_idea_stage_commit(
                    second_request.request_ref
                )
                is not None
            ):
                break
        assert second_request is not None
        assert (
            runtime.owners.advancement_engine.query_idea_stage_commit(
                second_request.request_ref
            )
            is not None
        )
        stranded = runtime.owners.agent_runtime.query_idea_stage_run(
            first_request.request_ref
        )
        assert stranded is not None
        assert stranded.execution is None

        provider.blocked_request_ref = None
        for _boundary in range(8):
            if not runtime.idea_stage.process_once():
                break
        assert (
            runtime.owners.advancement_engine.query_idea_stage_commit(
                first_request.request_ref
            )
            is not None
        )
        first_execution_count = sum(
            request.stage_request_ref == first_request.request_ref
            for request in provider.requests
        )
        second_execution_count = sum(
            request.stage_request_ref == second_request.request_ref
            for request in provider.requests
        )
        assert (first_execution_count, second_execution_count) == (1, 1)
    finally:
        runtime.close()


def test_one_worker_pass_attempts_at_most_one_provider_boundary(
    tmp_path: Path,
) -> None:
    data_root = prepare_data_root(tmp_path / "one-provider-boundary-per-pass")
    provider = _IdeaProvider()
    runtime = _runtime(data_root, provider)
    try:
        _confirm_question(runtime, "provider-budget-first")
        runtime.idea_stage.start("provider-budget-first-start")
        _confirm_question(runtime, "provider-budget-second")
        runtime.idea_stage.start("provider-budget-second-start")

        assert runtime.idea_stage.process_once()
        assert len(provider.requests) == 1
    finally:
        runtime.close()


def test_correcting_rejection_chain_cannot_starve_a_later_quest(
    tmp_path: Path,
) -> None:
    data_root = prepare_data_root(tmp_path / "multiple-quest-fairness")
    provider = _PerRequestRejectingIdeaProvider()
    runtime = _runtime(data_root, provider)
    try:
        first = _confirm_question(runtime, "rejecting-first")
        runtime.idea_stage.start("rejecting-first-stage-start")
        first_request = runtime.owners.advancement_engine.query_idea_stage_request(
            first["cycle_ref"]
        )
        assert first_request is not None
        provider.rejected_request_ref = first_request.request_ref

        second = _confirm_question(runtime, "fair-second")
        second_request = None
        for _boundary in range(10):
            assert runtime.idea_stage.process_once()
            second_request = (
                runtime.owners.advancement_engine.query_idea_stage_request(
                    second["cycle_ref"]
                )
            )
            if second_request is not None and (
                runtime.owners.advancement_engine.query_idea_stage_commit(
                    second_request.request_ref
                )
                is not None
            ):
                break
        assert second_request is not None
        assert (
            runtime.owners.advancement_engine.query_idea_stage_commit(
                second_request.request_ref
            )
            is not None
        )
        assert any(
            request.stage_request_ref == first_request.request_ref
            and request.submission_revision > 1
            for request in provider.requests
        )
    finally:
        runtime.close()


def test_web_worker_reports_transient_provider_failure_without_fake_receipt(
    tmp_path: Path,
) -> None:
    data_root = prepare_data_root(tmp_path / "provider-health-recovery")
    provider = _RecoveringIdeaProvider()
    runtime = _runtime(data_root, provider)
    _confirm_question(runtime)
    app = create_app(
        runtime, base_url="http://testserver", control_key="control-secret"
    )
    try:
        with TestClient(app, base_url="http://testserver") as client:
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                health = client.get(
                    "/internal/readiness",
                    headers={"X-Meta-Research-Control": "control-secret"},
                ).json()
                if health["idea_stage"]["status"] == "unavailable":
                    break
                time.sleep(0.02)
            assert health["idea_stage"] == {
                "status": "unavailable",
                "last_error": "codex_cli_unavailable",
            }

            token = runtime.authentication.issue_bootstrap_token()
            authenticated = client.post(
                "/auth/bootstrap",
                headers={"Origin": "http://testserver"},
                json={"token": token},
            )
            assert authenticated.status_code == 200
            blocked = client.get("/api/v1/snapshot").json()
            assert blocked["readiness"]["status"] == "ready"
            idea_check = next(
                check
                for check in blocked["readiness"]["checks"]
                if check["name"] == "idea_stage_worker"
            )
            assert idea_check == {
                "name": "idea_stage_worker",
                "status": "unavailable",
                "reason": {"code": "codex_cli_unavailable"},
            }
            assert blocked["idea_stage"]["run"]["status"] == "admitted"
            assert (
                blocked["idea_stage"]["run"]["attempt_execution_receipt"]
                is None
            )
            assert blocked["idea_stage"]["stage_commit"] is None

            provider.available = True
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                response = client.get("/api/v1/snapshot")
                if response.status_code != 200:
                    time.sleep(0.02)
                    continue
                recovered = response.json()
                if recovered["idea_stage"]["stage_commit"] is not None:
                    break
                time.sleep(0.02)
            assert recovered["idea_stage"]["stage_commit"]["status"] == (
                "Completed"
            )
            assert recovered["readiness"]["status"] == "ready"
            assert len(provider.requests) == 1
    finally:
        runtime.close()
