from __future__ import annotations

from dataclasses import replace
from importlib.resources import files
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

import pytest

from meta_research.idea_skill import (
    CodexIdeaSkillAdapter,
    IdeaSkillContractError,
    IdeaSkillDraft,
    IdeaSkillRequest,
    IdeaSkillResult,
    IdeaSkillUnavailable,
    _provider_hard_ceiling_error,
    validate_idea_skill_result,
)
from meta_research.idea_contract import (
    material_outcome_hash,
    validate_advisory_review,
)
from meta_research.owners.agent_runtime import IdeaRuntimeBinding
from meta_research.owners.common import canonical_hash
from meta_research.provider_supervisor import (
    CODEX_SUPERVISOR_REQUEST_SCHEMA_V2,
    read_transport_envelope,
    write_transport_envelope,
)
from meta_research.quest_drafting import (
    DraftingUnavailable,
    PROVIDER_RESULT_MAX_BYTES,
    PROVIDER_STREAM_MAX_BYTES,
)


def _runtime_binding() -> IdeaRuntimeBinding:
    return IdeaRuntimeBinding(
        packaged_skill_bundle_hash="1" * 64,
        instruction_set_hash="2" * 64,
        model_ref="test-model",
        harness_adapter_ref="test-harness",
        mcp_bindings=(),
        capability_bindings=("structured-output",),
        resource_bindings=("test-resource",),
    )


def _request(**changes: object) -> IdeaSkillRequest:
    context_pack: dict[str, object] = {
        "schema_ref": "meta-research/idea-context-pack/v1",
        "accepted_evidence_refs": ["asset:accepted-1"],
        "prior_accepted_outcomes": [],
        "active_guidance": [],
        "unknown_boundaries": ["跨设备稳健性未知"],
    }
    values: dict[str, object] = {
        "stage_request_ref": "stage-request:idea-1",
        "question_ref": "question:1",
        "context_pack_ref": "context-pack:1",
        "context_pack_hash": canonical_hash(context_pack),
        "context_pack": context_pack,
        "accepted_question_content": {"title": "保留稀有形态"},
        "root_session_ref": "run-session:1",
        "submission_revision": 1,
        "runtime_binding": _runtime_binding(),
    }
    values.update(changes)
    return IdeaSkillRequest(**values)  # type: ignore[arg-type]


def _idea_set(direction: str = "比较跨增强一致性与像素重建") -> dict[str, object]:
    return {
        "kind": "IdeaSet",
        "question_ref": "question:1",
        "context_pack_ref": "context-pack:1",
        "candidates": [
            {
                "candidate_key": "cross-augmentation",
                "direction": direction,
                "rationale": "结构一致性可能比逐像素误差更保留低频形态。",
                "assumptions": ["受控增强不改变稀有形态拓扑。"],
                "risks": ["增强可能保留传感器伪影。"],
                "evidence_boundary": {
                    "accepted_evidence_refs": ["asset:accepted-1"],
                    "supported": "已接纳材料限定了低照度场景。",
                    "inferred": "一致性约束可能提高稀有形态召回。",
                    "unknown": "跨设备稳健性未知。",
                },
                "falsification_hint": {
                    "test": "比较形态召回与伪影率。",
                    "would_refute": "召回不升或伪影显著增加。",
                },
                "material_difference": {
                    "from_history": "历史中没有相同机制。",
                    "from_peers": "干预轴是结构一致性。",
                    "plan_commitment_change": "Plan 必须比较两类目标函数。",
                },
            }
        ],
        "recommendation": None,
    }


def _no_viable_candidate() -> dict[str, object]:
    return {
        "kind": "NoViableCandidate",
        "question_ref": "question:1",
        "context_pack_ref": "context-pack:1",
        "exploration_scope": "比较已获准 Evidence 支持的结构保持机制。",
        "candidate_families_considered": [
            {
                "family": "跨增强结构一致性",
                "why_not_viable": "当前 Evidence 没有可识别稀有形态的标注或代理信号。",
                "evidence_refs": ["asset:accepted-1"],
            }
        ],
        "evidence_boundary": {
            "accepted_evidence_refs": ["asset:accepted-1"],
            "supported": "已接纳材料只限定了低照度场景。",
            "inferred": "现有代理目标不足以支持可负责的候选。",
            "unknown": "补充形态标注后能否形成候选仍未知。",
        },
        "overturn_conditions": ["接纳包含稀有形态标注的新 Evidence。"],
        "why_plan_cannot_proceed": "当前没有可冻结为实验承诺的可证伪机制。",
    }


def _result(
    *,
    draft: dict[str, object] | None = None,
    final: dict[str, object] | None = None,
    findings: tuple[dict[str, str], ...] = (),
    dispositions: tuple[dict[str, str], ...] = (),
    review_mode: str = "advisory_unobserved",
    reviewer_agent_ref: str | None = None,
) -> IdeaSkillResult:
    draft = draft or _idea_set()
    return IdeaSkillResult(
        reviewed_draft=draft,
        final_outcome=final or draft,
        findings=findings,
        dispositions=dispositions,
        primary_session_ref="codex-primary:1",
        review_mode=review_mode,
        reviewer_agent_ref=reviewer_agent_ref,
        adapter_kind="test",
    )


def _review_turn_output(
    *,
    findings: list[dict[str, str]] | None = None,
    final_outcome: dict[str, object] | None = None,
    dispositions: list[dict[str, str]] | None = None,
    reviewer_agent_ref: str = "codex-child-reviewer:1",
) -> dict[str, object]:
    del reviewer_agent_ref
    return {
        "findings": findings or [],
        "final_outcome": final_outcome or _idea_set(),
        "dispositions": dispositions or [],
    }


def test_canonical_skill_is_an_installed_runtime_resource() -> None:
    package = files("meta_research.skills.idea_stage")

    skill = (package / "SKILL.md").read_text(encoding="utf-8")
    contract = (package / "references" / "contract.md").read_text(encoding="utf-8")

    assert "execution completed != content accepted != domain accepted" in skill
    assert "NoViableCandidate" in contract
    assert "canonical selected Idea" in skill


def test_runtime_binding_fixes_harness_artifact_and_output_contract(
    tmp_path: Path,
) -> None:
    first = tmp_path / "codex-first"
    second = tmp_path / "codex-second"
    first.write_text(
        "#!/bin/sh\nprintf 'codex-test 1\\n'\n",
        encoding="utf-8",
    )
    second.write_text(
        "#!/bin/sh\nprintf 'codex-test 2\\n'\n",
        encoding="utf-8",
    )
    first.chmod(0o700)
    second.chmod(0o700)

    first_binding = CodexIdeaSkillAdapter(
        tmp_path / "first-workspace", executable=str(first)
    ).runtime_binding()
    second_binding = CodexIdeaSkillAdapter(
        tmp_path / "second-workspace", executable=str(second)
    ).runtime_binding()

    assert first_binding.harness_adapter_ref != second_binding.harness_adapter_ref
    assert "entry=" in first_binding.harness_adapter_ref
    assert "identity=artifact-manifest" in first_binding.harness_adapter_ref
    assert "version=" not in first_binding.harness_adapter_ref
    assert "artifact_manifest_sha256=" in first_binding.harness_adapter_ref
    assert any(
        binding.startswith("harness-artifact:")
        for binding in first_binding.resource_bindings
    )
    assert first_binding.mcp_bindings == ()
    assert "trusted-local-quest-authorization" in first_binding.capability_bindings
    assert "filesystem-danger-full-access" in first_binding.capability_bindings
    assert "shell-tool-enabled" in first_binding.capability_bindings
    assert "web-search-live" in first_binding.capability_bindings
    assert "native-subagent-enabled" in first_binding.capability_bindings
    assert any(
        binding == "runtime-policy:trusted-local-broad/v1"
        for binding in first_binding.resource_bindings
    )
    assert any(
        binding == "codex-config:web_search=live"
        for binding in first_binding.resource_bindings
    )
    assert "codex-config:features.multi_agent=true" in (
        first_binding.resource_bindings
    )
    assert any(
        binding == "sandbox-policy:danger-full-access"
        for binding in first_binding.resource_bindings
    )
    assert any(
        binding.startswith("transport-seal-key:sha256:")
        for binding in first_binding.resource_bindings
    )
    assert any(
        binding.startswith("output-schema:outcome-envelope-template@sha256:")
        for binding in first_binding.resource_bindings
    )
    assert "output-route:codex-output-last-message/json-schema/v1" in (
        first_binding.resource_bindings
    )

    downstream = tmp_path / "codex-downstream"
    wrapper = tmp_path / "codex-wrapper"
    downstream.write_text(
        "#!/bin/sh\nprintf 'codex-stable 1\\n'\n# behavior-a\n",
        encoding="utf-8",
    )
    wrapper.write_text(
        f"#!/bin/sh\nexec '{downstream}' \"$@\"\n",
        encoding="utf-8",
    )
    downstream.chmod(0o700)
    wrapper.chmod(0o700)
    before = CodexIdeaSkillAdapter(
        tmp_path / "wrapper-before", executable=str(wrapper)
    ).runtime_binding()
    downstream.write_text(
        "#!/bin/sh\nprintf 'codex-stable 1\\n'\n# behavior-b\n",
        encoding="utf-8",
    )
    downstream.chmod(0o700)
    after = CodexIdeaSkillAdapter(
        tmp_path / "wrapper-after", executable=str(wrapper)
    ).runtime_binding()
    assert before.harness_adapter_ref != after.harness_adapter_ref


def test_validator_accepts_a_reviewed_idea_set_with_exact_evidence() -> None:
    draft_hash, outcome_hash, review_hash = validate_idea_skill_result(
        _request(), _result()
    )

    assert draft_hash == outcome_hash == canonical_hash(_idea_set())
    assert review_hash


def test_validator_accepts_a_material_revision_with_a_revised_disposition() -> None:
    finding = {
        "finding_id": "finding-1",
        "category": "falsifiability",
        "message": "增加可推翻条件。",
    }
    revised = _idea_set("比较带预注册推翻阈值的结构一致性与像素重建")

    draft_hash, outcome_hash, _review_hash = validate_idea_skill_result(
        _request(),
        _result(
            final=revised,
            findings=(finding,),
            dispositions=(
                {
                    "finding_id": "finding-1",
                    "action": "revised",
                    "rationale": "已增加可推翻阈值。",
                },
            ),
        ),
    )

    assert draft_hash != outcome_hash


def test_validator_rejects_revised_disposition_without_a_material_revision() -> None:
    finding = {
        "finding_id": "finding-1",
        "category": "falsifiability",
        "message": "增加可推翻条件。",
    }

    with pytest.raises(IdeaSkillContractError, match="review_revision_not_material"):
        validate_idea_skill_result(
            _request(),
            _result(
                findings=(finding,),
                dispositions=(
                    {
                        "finding_id": "finding-1",
                        "action": "revised",
                        "rationale": "声称已修订。",
                    },
                ),
            ),
        )


def test_validator_rejects_changed_outcome_without_a_revised_disposition() -> None:
    revised = _idea_set("比较带预注册推翻阈值的结构一致性与像素重建")

    with pytest.raises(
        IdeaSkillContractError,
        match="review_outcome_changed_without_revision",
    ):
        validate_idea_skill_result(_request(), _result(final=revised))


def test_validator_keeps_historical_v1_review_payloads_readable() -> None:
    reviewed_draft_hash = canonical_hash(_idea_set())
    outcome_hash = canonical_hash(
        _idea_set("历史 v1 曾允许 reviewer 后的 Outcome 与草稿不同")
    )

    review_hash = validate_advisory_review(
        {
            "schema_ref": "meta-research/idea-advisory-review/v1",
            "reviewer_session_ref": "historical-reviewer-session:1",
            "reviewed_draft_hash": reviewed_draft_hash,
            "findings": [],
            "dispositions": [],
            "final_outcome_hash": outcome_hash,
            "independent": True,
            "advisory_only": True,
        },
        outcome_hash=outcome_hash,
        reviewed_draft_hash=reviewed_draft_hash,
    )

    assert review_hash


def test_validator_accepts_no_viable_candidate_as_a_real_outcome() -> None:
    outcome = _no_viable_candidate()

    _draft_hash, outcome_hash, _review_hash = validate_idea_skill_result(
        _request(), _result(draft=outcome)
    )

    assert outcome_hash == canonical_hash(outcome)


def test_complete_idea_set_rejects_materially_duplicate_candidates() -> None:
    outcome = _idea_set()
    duplicate = json.loads(json.dumps(outcome["candidates"][0]))
    duplicate["candidate_key"] = "identity-only-copy"
    duplicate["material_difference"]["from_peers"] = (
        "自述为不同候选不能构成研究实质差异。"
    )
    outcome["candidates"].append(duplicate)

    with pytest.raises(
        IdeaSkillContractError, match="idea_candidate_material_duplicate"
    ):
        validate_idea_skill_result(_request(), _result(draft=outcome))


def test_validator_rejects_a_claimed_internal_reviewer_identity() -> None:
    with pytest.raises(IdeaSkillContractError, match="idea_review_mode_invalid"):
        validate_idea_skill_result(
            _request(), _result(reviewer_agent_ref="run-session:1")
        )


def test_validator_rejects_unaccepted_evidence_refs() -> None:
    outcome = _idea_set()
    candidate = outcome["candidates"][0]  # type: ignore[index]
    candidate["evidence_boundary"]["accepted_evidence_refs"] = [  # type: ignore[index]
        "asset:not-accepted"
    ]

    with pytest.raises(IdeaSkillContractError, match="accepted_evidence_ref_unbound"):
        validate_idea_skill_result(_request(), _result(draft=outcome))


def test_owner_rejection_requires_a_material_successor_with_exact_lineage() -> None:
    predecessor = _idea_set()
    predecessor_hash = material_outcome_hash(predecessor)
    request = _request(
        submission_revision=2,
        native_session_ref="codex-primary:1",
        predecessor_submission_ref="submission:1",
        owner_rejection_receipt_ref="receipt:rg-rejection-1",
        owner_rejection_kind="domain",
        owner_feedback=("候选只是复述 Question，必须增加可检验干预轴。",),
    )

    with pytest.raises(
        IdeaSkillContractError, match="owner_feedback_revision_not_material"
    ):
        validate_idea_skill_result(
            request,
            _result(),
            predecessor_material_outcome_hash=predecessor_hash,
        )

    successor = _idea_set("比较形态拓扑保持约束与像素重建的可反驳差异")
    validate_idea_skill_result(
        request,
        _result(draft=successor),
        predecessor_material_outcome_hash=predecessor_hash,
    )


class _SequenceRunner:
    def __init__(
        self,
        outputs: list[dict[str, object]],
        *,
        thread_ids: list[str] | None = None,
        emit_review_spawn: bool = True,
        emit_review_timeout_wait: bool = False,
        emit_review_wait: bool = True,
        observed_reviewer_agent_ref: str | None = None,
        emit_primary_review_trace: bool = False,
    ) -> None:
        self._outputs = iter(outputs)
        self._thread_ids = None if thread_ids is None else iter(thread_ids)
        self._emit_review_spawn = emit_review_spawn
        self._emit_review_timeout_wait = emit_review_timeout_wait
        self._emit_review_wait = emit_review_wait
        self._observed_reviewer_agent_ref = observed_reviewer_agent_ref
        self._emit_primary_review_trace = emit_primary_review_trace
        self.calls: list[tuple[list[str], str, dict[str, object]]] = []

    def __call__(
        self, argv: list[str], prompt: str, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        del timeout
        schema_path = Path(argv[argv.index("--output-schema") + 1])
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        output_path = Path(argv[argv.index("--output-last-message") + 1])
        output = next(self._outputs)
        output_path.write_text(json.dumps(output, ensure_ascii=False), encoding="utf-8")
        thread_id = (
            next(self._thread_ids)
            if self._thread_ids is not None
            else "codex-primary:1"
        )
        self.calls.append((argv, prompt, schema))
        events: list[dict[str, object]] = [
            {"type": "thread.started", "thread_id": thread_id}
        ]
        if self._emit_primary_review_trace and "outcome" in schema.get(
            "properties", {}
        ):
            reviewer_agent_ref = "codex-primary-phase-reviewer:1"
            events.extend(
                (
                    _collab_event(
                        tool="spawn_agent",
                        sender_thread_id=thread_id,
                        reviewer_agent_ref=reviewer_agent_ref,
                        agent_status="pending_init",
                    ),
                    _collab_event(
                        tool="wait",
                        sender_thread_id=thread_id,
                        reviewer_agent_ref=reviewer_agent_ref,
                        agent_status="completed",
                    ),
                )
            )
        if "reviewer_agent_ref" in schema.get("properties", {}):
            reviewer_agent_ref = self._observed_reviewer_agent_ref or output.get(
                "reviewer_agent_ref"
            )
            assert isinstance(reviewer_agent_ref, str)
            if self._emit_review_spawn:
                events.append(
                    _collab_event(
                        tool="spawn_agent",
                        sender_thread_id=thread_id,
                        reviewer_agent_ref=reviewer_agent_ref,
                        agent_status="pending_init",
                    )
                )
            if self._emit_review_timeout_wait:
                events.append(
                    {
                        "type": "item.completed",
                        "item": {
                            "id": "collab-wait-timeout:1",
                            "type": "collab_tool_call",
                            "tool": "wait",
                            "sender_thread_id": thread_id,
                            "receiver_thread_ids": [],
                            "prompt": None,
                            "agents_states": {},
                            "status": "completed",
                        },
                    }
                )
            if self._emit_review_wait:
                events.append(
                    _collab_event(
                        tool="wait",
                        sender_thread_id=thread_id,
                        reviewer_agent_ref=reviewer_agent_ref,
                        agent_status="completed",
                    )
                )
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout="\n".join(
                json.dumps(event, ensure_ascii=False) for event in events
            ),
            stderr="",
        )

    def run_job(
        self, job_ref: str, argv: list[str], prompt: str, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        del job_ref
        return self(argv, prompt, timeout)


class _InvalidJsonRunner(_SequenceRunner):
    def __call__(
        self, argv: list[str], prompt: str, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        completed = super().__call__(argv, prompt, timeout)
        output_path = Path(argv[argv.index("--output-last-message") + 1])
        output_path.write_text("{not-json", encoding="utf-8")
        return completed


class _NonFiniteJsonRunner(_SequenceRunner):
    def __call__(
        self, argv: list[str], prompt: str, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        completed = super().__call__(argv, prompt, timeout)
        output_path = Path(argv[argv.index("--output-last-message") + 1])
        output_path.write_text('{"outcome":NaN}', encoding="utf-8")
        return completed


class _OversizedFeatureProbeRunner(_SequenceRunner):
    def run_command(
        self, argv: list[str], timeout: float
    ) -> subprocess.CompletedProcess[str]:
        del argv, timeout
        raise DraftingUnavailable("codex_output_too_large")


class _Codex0147LossyReviewRunner:
    """Record the production 0.147 ledger shape hidden by exec stdout."""

    root_session_ref = "01a04b6b-36e8-73a2-bc36-6996523d5fdb"
    child_session_ref = "01a04b71-2c39-7cf3-b8d2-863b5742ed73"
    reviewer_task_name = "idea_review_bd8f646f"
    reviewer_task_path = f"/root/{reviewer_task_name}"
    primary_turn_id = "01a04b6b-3724-78a3-b77e-fe603857db73"
    review_turn_id = "01a04b6e-e5ba-70d2-9fbb-ac9c0e87b846"
    child_turn_id = "01a04b71-2c66-7031-83e2-1c0ed94f8bd9"

    def __init__(self, codex_home: Path) -> None:
        self._codex_home = codex_home
        self.job_refs: list[str] = []

    def __call__(
        self, argv: list[str], prompt: str, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        raise AssertionError("the regression must use the durable job seam")

    def run_job(
        self, job_ref: str, argv: list[str], prompt: str, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        del timeout
        self.job_refs.append(job_ref)
        assert argv[-3:] == ["resume", self.root_session_ref, "-"]
        output_path = Path(argv[argv.index("--output-last-message") + 1])
        output = _review_turn_output(reviewer_agent_ref=self.reviewer_task_path)
        result_json = json.dumps(
            output,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        output_path.write_text(result_json, encoding="utf-8")

        empty_wait = {
            "id": "item_1",
            "type": "collab_tool_call",
            "tool": "wait",
            "sender_thread_id": self.root_session_ref,
            "receiver_thread_ids": [],
            "prompt": None,
            "agents_states": {},
        }
        lossy_stdout = (
            {"type": "thread.started", "thread_id": self.root_session_ref},
            {"type": "turn.started"},
            {
                "type": "item.completed",
                "item": {
                    "id": "item_0",
                    "type": "agent_message",
                    "text": "Starting the independent review.",
                },
            },
            {"type": "item.started", "item": {**empty_wait, "status": "in_progress"}},
            {"type": "item.completed", "item": {**empty_wait, "status": "completed"}},
            {
                "type": "item.completed",
                "item": {
                    "id": "item_2",
                    "type": "agent_message",
                    "text": result_json,
                },
            },
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 147,
                    "cached_input_tokens": 0,
                    "output_tokens": 147,
                },
            },
        )
        assert len(lossy_stdout) == 7
        assert not any("spawn" in json.dumps(event) for event in lossy_stdout)
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout="\n".join(json.dumps(event) for event in lossy_stdout),
            stderr="",
        )

    def _write_ledgers(
        self,
        *,
        cwd: str,
        model: str,
        review_prompt: str,
        result_json: str,
        child_prompt: str,
        child_terminal: str,
    ) -> None:
        spawn_call_id = "call_wIt1xv0w7R7wMFBSFHRL5VHk"
        wait_call_id = "call_H0tSZvhUWCkmvR7X6IQpnLRX"
        root_records = (
            self._record(
                "2026-08-29T02:48:32.813Z",
                "session_meta",
                {
                    "id": self.root_session_ref,
                    "session_id": self.root_session_ref,
                    "timestamp": "2026-08-29T02:48:32.745Z",
                    "cwd": cwd,
                    "originator": "codex_exec",
                    "cli_version": "0.147.0",
                    "source": "exec",
                    "thread_source": "user",
                    "model_provider": "openai",
                    "base_instructions": {},
                    "history_mode": "legacy",
                    "context_window": {"window_id": self.root_session_ref},
                },
            ),
            self._record(
                "2026-08-29T02:48:40.165Z",
                "turn_context",
                self._turn_context(self.primary_turn_id, cwd=cwd, model=model),
            ),
            self._record(
                "2026-08-29T02:52:16.294Z",
                "event_msg",
                {
                    "type": "task_complete",
                    "turn_id": self.primary_turn_id,
                    "started_at": 1787971720,
                    "completed_at": 1787971936,
                    "duration_ms": 216_131,
                    "time_to_first_token_ms": 1_000,
                    "last_agent_message": json.dumps(
                        {"outcome": _idea_set()},
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                },
            ),
            self._record(
                "2026-08-29T02:52:34.111Z",
                "event_msg",
                {
                    "type": "task_started",
                    "turn_id": self.review_turn_id,
                    "started_at": 1787971954,
                    "model_context_window": 258_400,
                    "collaboration_mode_kind": "Default",
                },
            ),
            self._record(
                "2026-08-29T02:52:37.443Z",
                "turn_context",
                self._turn_context(self.review_turn_id, cwd=cwd, model=model),
            ),
            self._record(
                "2026-08-29T02:52:37.456Z",
                "response_item",
                {
                    "id": "review-user-message",
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": review_prompt}],
                },
            ),
            self._record(
                "2026-08-29T02:55:03.220Z",
                "response_item",
                {
                    "id": "spawn-reviewer",
                    "type": "function_call",
                    "name": "spawn_agent",
                    "namespace": "collaboration",
                    "call_id": spawn_call_id,
                    "arguments": json.dumps(
                        {
                            "fork_turns": "none",
                            "message": child_prompt,
                            "task_name": self.reviewer_task_name,
                        },
                        sort_keys=True,
                    ),
                },
            ),
            self._record(
                "2026-08-29T02:55:03.271Z",
                "event_msg",
                {
                    "type": "sub_agent_activity",
                    "kind": "started",
                    "event_id": spawn_call_id,
                    "agent_path": self.reviewer_task_path,
                    "agent_thread_id": self.child_session_ref,
                    "occurred_at_ms": 1_787_972_103_271,
                },
            ),
            self._record(
                "2026-08-29T02:55:03.275Z",
                "response_item",
                {
                    "id": "spawn-reviewer-output",
                    "type": "function_call_output",
                    "call_id": spawn_call_id,
                    "output": json.dumps({"task_name": self.reviewer_task_path}),
                },
            ),
            self._record(
                "2026-08-29T02:55:08.323Z",
                "response_item",
                {
                    "id": "wait-reviewer",
                    "type": "function_call",
                    "name": "wait_agent",
                    "namespace": "collaboration",
                    "call_id": wait_call_id,
                    "arguments": json.dumps({"timeout_ms": 120_000}),
                },
            ),
            self._record(
                "2026-08-29T02:56:52.923Z",
                "response_item",
                {
                    "id": "wait-reviewer-output",
                    "type": "function_call_output",
                    "call_id": wait_call_id,
                    "output": json.dumps(
                        {"message": "Wait completed.", "timed_out": False},
                        sort_keys=True,
                    ),
                },
            ),
            self._record(
                "2026-08-29T02:56:52.929Z",
                "response_item",
                {
                    "id": "reviewer-delivery",
                    "type": "agent_message",
                    "author": self.reviewer_task_path,
                    "recipient": "/root",
                    "content": [
                        {
                            "type": "input_text",
                            "text": (
                                "Message Type: FINAL_ANSWER\n"
                                "Task name: /root\n"
                                f"Sender: {self.reviewer_task_path}\n"
                                f"Payload:\n{child_terminal}"
                            ),
                        },
                        {
                            "type": "encrypted_content",
                            "encrypted_content": child_prompt,
                        },
                    ],
                },
            ),
            self._record(
                "2026-08-29T02:58:29.780Z",
                "response_item",
                {
                    "id": "review-result",
                    "type": "message",
                    "role": "assistant",
                    "phase": "final_answer",
                    "content": [{"type": "output_text", "text": result_json}],
                },
            ),
            self._record(
                "2026-08-29T02:58:29.800Z",
                "event_msg",
                {
                    "type": "task_complete",
                    "turn_id": self.review_turn_id,
                    "started_at": 1787971954,
                    "completed_at": 1787972309,
                    "duration_ms": 355_689,
                    "time_to_first_token_ms": 14_648,
                    "last_agent_message": result_json,
                },
            ),
        )
        child_records = (
            self._record(
                "2026-08-29T02:55:03.273Z",
                "session_meta",
                {
                    "id": self.child_session_ref,
                    "session_id": self.root_session_ref,
                    "parent_thread_id": self.root_session_ref,
                    "timestamp": "2026-08-29T02:55:03.225Z",
                    "cwd": cwd,
                    "originator": "codex_exec",
                    "cli_version": "0.147.0",
                    "source": {
                        "subagent": {
                            "thread_spawn": {
                                "parent_thread_id": self.root_session_ref,
                                "depth": 1,
                                "agent_path": self.reviewer_task_path,
                                "agent_nickname": "Tesla",
                                "agent_role": None,
                            }
                        }
                    },
                    "thread_source": "subagent",
                    "agent_nickname": "Tesla",
                    "agent_path": self.reviewer_task_path,
                    "model_provider": "openai",
                    "base_instructions": {},
                    "history_mode": "legacy",
                    "multi_agent_version": "v2",
                    "context_window": {"window_id": self.child_session_ref},
                },
            ),
            self._record(
                "2026-08-29T02:55:03.273Z",
                "event_msg",
                {
                    "type": "task_started",
                    "turn_id": self.child_turn_id,
                    "started_at": 1787972103,
                    "model_context_window": 258_400,
                    "collaboration_mode_kind": "Default",
                },
            ),
            self._record(
                "2026-08-29T02:55:06.367Z",
                "turn_context",
                self._turn_context(self.child_turn_id, cwd=cwd, model=model),
            ),
            self._record(
                "2026-08-29T02:55:06.370Z",
                "response_item",
                {
                    "id": "child-task-delivery",
                    "type": "agent_message",
                    "author": "/root",
                    "recipient": self.reviewer_task_path,
                    "content": [
                        {
                            "type": "input_text",
                            "text": (
                                "Message Type: NEW_TASK\n"
                                f"Task name: {self.reviewer_task_path}\n"
                                "Sender: /root\n"
                                f"Payload:\n{child_prompt}"
                            ),
                        }
                    ],
                },
            ),
            self._record(
                "2026-08-29T02:56:52.828Z",
                "response_item",
                {
                    "id": "child-result",
                    "type": "message",
                    "role": "assistant",
                    "phase": "final_answer",
                    "content": [
                        {"type": "output_text", "text": child_terminal}
                    ],
                },
            ),
            self._record(
                "2026-08-29T02:56:52.880Z",
                "event_msg",
                {
                    "type": "task_complete",
                    "turn_id": self.child_turn_id,
                    "started_at": 1787972103,
                    "completed_at": 1787972212,
                    "duration_ms": 109_607,
                    "time_to_first_token_ms": 14_471,
                    "last_agent_message": child_terminal,
                },
            ),
        )
        self._write_ledger(
            "2026/08/29/rollout-2026-08-29T02-48-32-"
            f"{self.root_session_ref}.jsonl",
            root_records,
        )
        self._write_ledger(
            "2026/08/29/rollout-2026-08-29T02-55-03-"
            f"{self.child_session_ref}.jsonl",
            child_records,
        )

    @staticmethod
    def _turn_context(turn_id: str, *, cwd: str, model: str) -> dict[str, object]:
        return {
            "turn_id": turn_id,
            "cwd": cwd,
            "model": model,
            "effort": "max",
            "approval_policy": "never",
            "sandbox_policy": {"type": "danger-full-access"},
            "permission_profile": {"type": "disabled"},
            "collaboration_mode": {
                "mode": "default",
                "settings": {
                    "model": model,
                    "reasoning_effort": "max",
                    "developer_instructions": None,
                },
            },
            "multi_agent_version": "v2",
            "workspace_roots": [cwd],
            "current_date": "2026-08-29",
            "timezone": "Etc/UTC",
        }

    @staticmethod
    def _record(
        timestamp: str, record_type: str, payload: dict[str, object]
    ) -> dict[str, object]:
        return {"timestamp": timestamp, "type": record_type, "payload": payload}

    def _write_ledger(
        self, relative_path: str, records: tuple[dict[str, object], ...]
    ) -> None:
        path = self._codex_home / "sessions" / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "\n".join(
                json.dumps(record, ensure_ascii=False, separators=(",", ":"))
                for record in records
            )
            + "\n",
            encoding="utf-8",
        )


def _assert_codex_schema_types(value: object, path: str = "$") -> None:
    """Match the provider's strict requirement for every const schema node."""

    if isinstance(value, list):
        for index, item in enumerate(value):
            _assert_codex_schema_types(item, f"{path}[{index}]")
        return
    if not isinstance(value, dict):
        return
    if "const" in value:
        assert "type" in value, f"missing type at {path}"
    for key, item in value.items():
        _assert_codex_schema_types(item, f"{path}.{key}")


def _collab_event(
    *,
    tool: str,
    sender_thread_id: str,
    reviewer_agent_ref: str,
    agent_status: str,
) -> dict[str, object]:
    return {
        "type": "item.completed",
        "item": {
            "id": f"collab-{tool}:1",
            "type": "collab_tool_call",
            "tool": tool,
            "sender_thread_id": sender_thread_id,
            "receiver_thread_ids": [reviewer_agent_ref],
            "agents_states": {
                reviewer_agent_ref: {
                    "status": agent_status,
                    "message": "review complete" if agent_status == "completed" else None,
                }
            },
            "status": "completed",
        },
    }


class _OutcomeUnknownRunner:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(
        self, argv: list[str], prompt: str, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        del argv, prompt
        self.calls += 1
        raise subprocess.TimeoutExpired("codex", timeout)

    def run_job(
        self, job_ref: str, argv: list[str], prompt: str, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        del job_ref
        return self(argv, prompt, timeout)


class _FeatureProbeOutcomeUnknownRunner(_OutcomeUnknownRunner):
    def __init__(self, feature_output: str | None) -> None:
        super().__init__()
        self._feature_output = feature_output

    def run_command(
        self, argv: list[str], timeout: float
    ) -> subprocess.CompletedProcess[str]:
        del timeout
        assert argv[-2:] == ["features", "list"]
        return subprocess.CompletedProcess(
            argv,
            0 if self._feature_output is not None else 1,
            stdout=self._feature_output or "",
            stderr="",
        )


class _ExitFailureRunner:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(
        self, argv: list[str], prompt: str, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        del prompt, timeout
        self.calls += 1
        output_path = Path(argv[argv.index("--output-last-message") + 1])
        output_path.write_text(
            json.dumps({"outcome": _idea_set()}, ensure_ascii=False),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(
            argv,
            17,
            stdout=json.dumps(
                {"type": "thread.started", "thread_id": "failed-primary"}
            ),
            stderr="provider failed after writing an output",
        )

    def run_job(
        self, job_ref: str, argv: list[str], prompt: str, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        del job_ref
        return self(argv, prompt, timeout)


class _DetachedSupervisorRunner:
    def __init__(self) -> None:
        self.process: subprocess.Popen[bytes] | None = None

    def __call__(
        self, argv: list[str], prompt: str, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        raise AssertionError("durable path required")

    def run_job(
        self, job_ref: str, argv: list[str], prompt: str, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        raise AssertionError("supervisor path required")

    def run_durable_job(
        self,
        job_ref: str,
        argv: list[str],
        prompt: str,
        timeout: float,
        stdout_path: Path,
        pid_path: Path,
        supervisor_request_path: Path,
    ) -> subprocess.CompletedProcess[str]:
        del job_ref, argv, prompt, timeout, stdout_path, pid_path
        self.process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "meta_research.provider_supervisor",
                str(supervisor_request_path),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        raise OSError("simulated daemon loss after supervisor launch")


class _PrelaunchLossRunner:
    def __call__(
        self, argv: list[str], prompt: str, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        raise AssertionError("durable path required")

    def run_job(
        self, job_ref: str, argv: list[str], prompt: str, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        raise AssertionError("supervisor path required")

    def run_durable_job(
        self,
        job_ref: str,
        argv: list[str],
        prompt: str,
        timeout: float,
        stdout_path: Path,
        pid_path: Path,
        supervisor_request_path: Path,
    ) -> subprocess.CompletedProcess[str]:
        del job_ref, argv, prompt, timeout, stdout_path, pid_path
        assert supervisor_request_path.is_file()
        raise OSError("simulated daemon loss before supervisor launch")


class _MultipleNativeSessionRunner(_SequenceRunner):
    def __call__(
        self, argv: list[str], prompt: str, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        completed = super().__call__(argv, prompt, timeout)
        return subprocess.CompletedProcess(
            completed.args,
            completed.returncode,
            stdout="\n".join(
                (
                    completed.stdout,
                    json.dumps(
                        {
                            "type": "thread.started",
                            "thread_id": "alien-native-session",
                        }
                    ),
                )
            ),
            stderr=completed.stderr,
        )


def _fake_codex_executable(
    path: Path,
    *,
    read_all_input: bool,
    sleep_seconds: float = 0,
) -> Path:
    read_expression = (
        "sys.stdin.buffer.read()"
        if read_all_input
        else "os.read(sys.stdin.fileno(), 1)"
    )
    encoded_outcome = repr(json.dumps({"outcome": _idea_set()}, ensure_ascii=False))
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        "import os\n"
        "from pathlib import Path\n"
        "import sys\n"
        "import time\n"
        "if '--version' in sys.argv:\n"
        "    print('codex-supervisor-test 1')\n"
        "    raise SystemExit(0)\n"
        f"{read_expression}\n"
        f"time.sleep({sleep_seconds!r})\n"
        "args = sys.argv[1:]\n"
        "result_path = Path(args[args.index('--output-last-message') + 1])\n"
        f"result_path.write_text({encoded_outcome}, encoding='utf-8')\n"
        "print(json.dumps({'type': 'thread.started', 'thread_id': 'supervised-primary'}))\n",
        encoding="utf-8",
    )
    path.chmod(0o700)
    return path


def _descendant_codex_executable(path: Path, child_pid_path: Path) -> Path:
    encoded_outcome = repr(json.dumps({"outcome": _idea_set()}, ensure_ascii=False))
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        "from pathlib import Path\n"
        "import subprocess\n"
        "import sys\n"
        "if '--version' in sys.argv:\n"
        "    print('codex-descendant-test 1')\n"
        "    raise SystemExit(0)\n"
        "sys.stdin.buffer.read()\n"
        "child = subprocess.Popen([sys.executable, '-c', "
        "'import time; time.sleep(30)'])\n"
        f"Path({str(child_pid_path)!r}).write_text(str(child.pid))\n"
        "args = sys.argv[1:]\n"
        "result_path = Path(args[args.index('--output-last-message') + 1])\n"
        f"result_path.write_text({encoded_outcome}, encoding='utf-8')\n"
        "print(json.dumps({'type': 'thread.started', "
        "'thread_id': 'descendant-primary'}))\n",
        encoding="utf-8",
    )
    path.chmod(0o700)
    return path


def test_production_adapter_runs_packaged_skill_with_canonical_capabilities(
    tmp_path: Path,
) -> None:
    runner = _SequenceRunner(
        [{"outcome": _idea_set()}, _review_turn_output()]
    )
    adapter = CodexIdeaSkillAdapter(
        tmp_path / "idea-provider", process_runner=runner
    )

    request = _request(runtime_binding=adapter.runtime_binding())
    result = adapter.execute(request)
    validate_idea_skill_result(request, result)

    assert result.primary_session_ref == "codex-primary:1"
    assert result.review_mode == "advisory_unobserved"
    assert result.reviewer_agent_ref is None
    assert result.adapter_kind == "codex_cli"
    assert len(runner.calls) == 2
    primary_argv, primary_prompt, primary_schema = runner.calls[0]
    review_argv, review_prompt, review_schema = runner.calls[1]
    assert primary_argv[:2] == ["codex", "exec"]
    assert primary_argv[2:4] == ["--enable", "multi_agent"]
    assert "--ignore-user-config" not in primary_argv
    assert "--ignore-rules" not in primary_argv
    config_values = {
        primary_argv[index + 1]
        for index, value in enumerate(primary_argv[:-1])
        if value == "--config"
    }
    assert 'approval_policy="never"' in config_values
    assert 'model_reasoning_effort="max"' in config_values
    assert 'web_search="live"' in config_values
    assert primary_argv[primary_argv.index("--sandbox") + 1] == "danger-full-access"
    agent_workspace = Path(primary_argv[primary_argv.index("--cd") + 1])
    schema_path = Path(primary_argv[primary_argv.index("--output-schema") + 1])
    assert agent_workspace.name == "research-workspace"
    assert not schema_path.is_relative_to(agent_workspace)
    assert primary_argv[primary_argv.index("--model") + 1] == "gpt-5.6-sol"
    assert (
        "codex-config:model_reasoning_effort=max"
        in request.runtime_binding.resource_bindings
    )
    disabled = {
        primary_argv[index + 1]
        for index, value in enumerate(primary_argv[:-1])
        if value == "--disable"
    }
    assert {"apps", "browser_use", "computer_use"} <= disabled
    assert "plugins" not in disabled
    assert "remote_plugin" not in disabled
    enabled = {
        primary_argv[index + 1]
        for index, value in enumerate(primary_argv[:-1])
        if value == "--enable"
    }
    assert {"multi_agent", "plugins", "remote_plugin", "hooks"} <= enabled
    assert "shell_tool" not in disabled
    assert "skill_search" not in disabled
    assert "view_image" not in disabled
    assert "execution completed != content accepted" in primary_prompt
    assert "## IdeaStageInvocation" in primary_prompt
    assert "## Accepted handoff" in primary_prompt
    assert "一个 submission identity" in primary_prompt
    assert "不得创建 Question、Plan、Run、receipt" in primary_prompt
    assert set(primary_schema["properties"]) == {"outcome"}
    assert "anyOf" in primary_schema["properties"]["outcome"]
    _assert_codex_schema_types(primary_schema)
    assert "## IdeaStageInvocation" in review_prompt
    assert "## Accepted handoff" in review_prompt
    assert "reviewed_draft=" in review_prompt
    assert review_argv[-3:] == ["resume", "codex-primary:1", "-"]
    assert set(review_schema["properties"]) == {
        "findings",
        "final_outcome",
        "dispositions",
    }
    _assert_codex_schema_types(review_schema)


def test_completion_rejection_feedback_reaches_both_idea_provider_turns(
    tmp_path: Path,
) -> None:
    successor = _idea_set()
    runner = _SequenceRunner(
        [
            {"outcome": successor},
            _review_turn_output(final_outcome=successor),
        ]
    )
    adapter = CodexIdeaSkillAdapter(
        tmp_path / "idea-successor-provider",
        process_runner=runner,
    )
    request = _request(
        runtime_binding=adapter.runtime_binding(),
        submission_revision=2,
        native_session_ref="codex-primary:1",
        predecessor_submission_ref="idea-candidate:rejected",
        owner_rejection_receipt_ref="receipt:idea-completion-rejected",
        owner_rejection_kind="completion",
        owner_feedback=("修正 final_outcome 的 evidence boundary 表达。",),
    )

    result = adapter.execute(request)
    validate_idea_skill_result(request, result)

    assert len(runner.calls) == 2
    for _argv, prompt, _schema in runner.calls:
        assert "owner_rejection_kind=completion" in prompt
        assert "idea-candidate:rejected" in prompt
        assert "receipt:idea-completion-rejected" in prompt
        assert "修正 final_outcome 的 evidence boundary 表达" in prompt


def test_invalid_json_is_preserved_as_a_rejected_completion_candidate(
    tmp_path: Path,
) -> None:
    runner = _InvalidJsonRunner([{"outcome": _idea_set()}])
    adapter = CodexIdeaSkillAdapter(
        tmp_path / "idea-invalid-json",
        process_runner=runner,
    )

    with pytest.raises(IdeaSkillUnavailable) as caught:
        adapter.generate_draft(
            _request(
                runtime_binding=adapter.runtime_binding(),
                job_ref="idea-invalid-json-job",
            )
        )

    error = caught.value
    assert error.code == "idea_primary_result_contract_invalid"
    assert error.rejected_native_session_ref == "codex-primary:1"
    assert error.rejected_detail_code == "codex_output_invalid"
    assert error.recovery_checkpoint is not None
    assert error.rejected_candidate == {
        "schema_ref": "meta-research/raw-provider-candidate/v1",
        "encoding": "utf-8",
        "content": "{not-json",
        "content_hash": error.recovery_checkpoint["result_file_hash"],
    }


def test_non_finite_json_is_preserved_as_a_rejected_completion_candidate(
    tmp_path: Path,
) -> None:
    runner = _NonFiniteJsonRunner([{"outcome": _idea_set()}])
    adapter = CodexIdeaSkillAdapter(
        tmp_path / "idea-non-finite-json",
        process_runner=runner,
    )

    with pytest.raises(IdeaSkillUnavailable) as caught:
        adapter.generate_draft(
            _request(
                runtime_binding=adapter.runtime_binding(),
                job_ref="idea-non-finite-json-job",
            )
        )

    error = caught.value
    assert error.code == "idea_primary_result_contract_invalid"
    assert error.rejected_native_session_ref == "codex-primary:1"
    assert error.rejected_detail_code == "codex_output_invalid"
    assert error.recovery_checkpoint is not None
    assert error.rejected_candidate == {
        "schema_ref": "meta-research/raw-provider-candidate/v1",
        "encoding": "utf-8",
        "content": '{"outcome":NaN}',
        "content_hash": error.recovery_checkpoint["result_file_hash"],
    }


def test_production_adapter_does_not_require_a_review_spawn_trace(
    tmp_path: Path,
) -> None:
    runner = _SequenceRunner(
        [{"outcome": _idea_set()}, _review_turn_output()],
        emit_review_spawn=False,
    )
    adapter = CodexIdeaSkillAdapter(
        tmp_path / "review-missing-spawn",
        process_runner=runner,
    )

    result = adapter.execute(_request(runtime_binding=adapter.runtime_binding()))

    assert result.reviewer_agent_ref is None


def test_production_adapter_does_not_require_a_review_wait_trace(
    tmp_path: Path,
) -> None:
    runner = _SequenceRunner(
        [{"outcome": _idea_set()}, _review_turn_output()],
        emit_review_wait=False,
    )
    adapter = CodexIdeaSkillAdapter(
        tmp_path / "review-missing-wait",
        process_runner=runner,
    )

    result = adapter.execute(_request(runtime_binding=adapter.runtime_binding()))

    assert result.reviewer_agent_ref is None


def test_production_adapter_accepts_timeout_then_terminal_wait(
    tmp_path: Path,
) -> None:
    runner = _SequenceRunner(
        [{"outcome": _idea_set()}, _review_turn_output()],
        emit_review_timeout_wait=True,
    )
    adapter = CodexIdeaSkillAdapter(
        tmp_path / "review-timeout-then-completed",
        process_runner=runner,
    )

    result = adapter.execute(_request(runtime_binding=adapter.runtime_binding()))

    assert result.reviewer_agent_ref is None


def test_codex_0147_lossy_stdout_does_not_gate_public_review_result(
    tmp_path: Path,
) -> None:
    codex_home = (tmp_path / "codex-home").absolute()
    runner = _Codex0147LossyReviewRunner(codex_home)
    adapter = CodexIdeaSkillAdapter(
        tmp_path / "idea-provider",
        process_runner=runner,
        codex_home=codex_home,
    )
    request = _request(
        runtime_binding=adapter.runtime_binding(),
        native_session_ref=runner.root_session_ref,
        job_ref="idea-review-operation:codex-0147-lossy-stdout",
    )
    draft = IdeaSkillDraft(
        draft=_idea_set(),
        primary_session_ref=runner.root_session_ref,
        adapter_kind="codex_cli",
    )

    result = adapter.review_draft(request, draft)

    assert result.reviewer_agent_ref is None
    assert result.primary_session_ref == runner.root_session_ref
    assert runner.job_refs == [request.job_ref]


def test_production_adapter_does_not_accept_a_provider_reviewer_identity(
    tmp_path: Path,
) -> None:
    runner = _SequenceRunner(
        [{"outcome": _idea_set()}, _review_turn_output()],
        observed_reviewer_agent_ref="actual-codex-child:1",
    )
    adapter = CodexIdeaSkillAdapter(
        tmp_path / "review-forged-child-ref",
        process_runner=runner,
    )

    result = adapter.execute(_request(runtime_binding=adapter.runtime_binding()))

    assert result.reviewer_agent_ref is None


def test_durable_review_recovery_does_not_require_child_trace(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "durable-review-missing-wait"
    runner = _SequenceRunner(
        [{"outcome": _idea_set()}, _review_turn_output()],
        emit_review_wait=False,
    )
    first = CodexIdeaSkillAdapter(workspace, process_runner=runner)
    request = _request(
        runtime_binding=first.runtime_binding(),
        job_ref="idea-review-operation:invalid-child-trace",
    )
    draft = first.generate_draft(request)
    review_request = replace(
        request,
        native_session_ref=draft.primary_session_ref,
    )

    first_result = first.review_draft(review_request, draft)

    no_replay = _SequenceRunner([])
    restarted = CodexIdeaSkillAdapter(workspace, process_runner=no_replay)
    recovered = restarted.review_draft(review_request, draft)

    assert recovered.final_outcome == first_result.final_outcome
    assert no_replay.calls == []


def test_durable_primary_ignores_internal_review_trace_on_replay(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "durable-primary-early-review"
    runner = _SequenceRunner(
        [{"outcome": _idea_set()}],
        emit_primary_review_trace=True,
    )
    first = CodexIdeaSkillAdapter(workspace, process_runner=runner)
    request = _request(
        runtime_binding=first.runtime_binding(),
        job_ref="idea-primary-operation:early-review",
    )

    first_draft = first.generate_draft(request)

    no_replay = _SequenceRunner([])
    restarted = CodexIdeaSkillAdapter(workspace, process_runner=no_replay)
    recovered = restarted.generate_draft(request)

    assert recovered.draft == first_draft.draft
    assert no_replay.calls == []


def test_durable_review_without_spawn_trace_replays_public_result(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "durable-review-invalid-trace"
    runner = _SequenceRunner(
        [{"outcome": _idea_set()}, _review_turn_output()],
        emit_review_spawn=False,
    )
    first = CodexIdeaSkillAdapter(workspace, process_runner=runner)
    request = _request(
        runtime_binding=first.runtime_binding(),
        job_ref="idea-review-operation:invalid-sealed-trace",
    )
    draft = first.generate_draft(request)
    review_request = replace(
        request,
        native_session_ref=draft.primary_session_ref,
    )

    first_result = first.review_draft(review_request, draft)

    no_replay = _SequenceRunner([])
    restarted = CodexIdeaSkillAdapter(workspace, process_runner=no_replay)
    recovered = restarted.review_draft(review_request, draft)

    assert recovered.final_outcome == first_result.final_outcome
    assert no_replay.calls == []


def test_production_adapter_resumes_the_root_session_for_review_dispositions(
    tmp_path: Path,
) -> None:
    finding = {
        "finding_id": "finding-1",
        "category": "falsifiability",
        "message": "候选需要更明确的推翻条件。",
    }
    final = _idea_set("比较带预注册推翻阈值的结构一致性与像素重建")
    runner = _SequenceRunner(
        [
            {"outcome": _idea_set()},
            _review_turn_output(
                findings=[finding],
                final_outcome=final,
                dispositions=[
                    {
                        "finding_id": "finding-1",
                        "action": "revised",
                        "rationale": "已把推翻阈值写入候选方向与 falsification hint。",
                    }
                ],
            ),
        ]
    )
    adapter = CodexIdeaSkillAdapter(
        tmp_path / "idea-provider-review", process_runner=runner
    )

    request = _request(runtime_binding=adapter.runtime_binding())
    result = adapter.execute(request)
    validate_idea_skill_result(request, result)

    assert result.final_outcome == final
    assert result.findings == (finding,)
    assert result.dispositions[0]["action"] == "revised"
    assert len(runner.calls) == 2
    review_argv, review_prompt, review_schema = runner.calls[1]
    assert review_argv[-3:] == ["resume", "codex-primary:1", "-"]
    assert "根 Idea Agent" in review_prompt
    assert set(review_schema["properties"]) == {
        "findings",
        "final_outcome",
        "dispositions",
    }


def test_production_adapter_rejects_a_resume_that_changes_native_session(
    tmp_path: Path,
) -> None:
    finding = {
        "finding_id": "finding-1",
        "category": "falsifiability",
        "message": "候选需要更明确的推翻条件。",
    }
    runner = _SequenceRunner(
        [
            {"outcome": _idea_set()},
            _review_turn_output(
                findings=[finding],
                final_outcome=_idea_set("加入可反驳阈值的结构一致性"),
                dispositions=[
                    {
                        "finding_id": "finding-1",
                        "action": "revised",
                        "rationale": "加入了推翻阈值。",
                    }
                ],
            ),
        ],
        thread_ids=[
            "codex-primary:1",
            "alien-native-session",
        ],
    )
    adapter = CodexIdeaSkillAdapter(
        tmp_path / "native-session-mismatch",
        process_runner=runner,
    )
    request = _request(runtime_binding=adapter.runtime_binding())

    with pytest.raises(
        IdeaSkillUnavailable,
        match="codex_native_session_mismatch",
    ):
        adapter.execute(request)

    assert runner.calls[1][0][-3:] == ["resume", "codex-primary:1", "-"]


def test_production_adapter_reconciles_each_durable_provider_phase(
    tmp_path: Path,
) -> None:
    """A response lost before the Owner commit must not invoke Codex again."""

    workspace = tmp_path / "durable-idea-provider"
    finding = {
        "finding_id": "durable-finding",
        "category": "falsifiability",
        "message": "补充可反驳阈值。",
    }
    revised = _idea_set("比较带预注册阈值的结构一致性与像素重建")
    first_runner = _SequenceRunner(
        [
            {"outcome": _idea_set()},
            _review_turn_output(
                findings=[finding],
                final_outcome=revised,
                dispositions=[
                    {
                        "finding_id": "durable-finding",
                        "action": "revised",
                        "rationale": "已补充预注册阈值。",
                    }
                ],
            ),
        ]
    )
    first = CodexIdeaSkillAdapter(workspace, process_runner=first_runner)
    primary_request = _request(
        runtime_binding=first.runtime_binding(),
        job_ref="idea-primary-operation:stable",
    )

    draft = first.generate_draft(primary_request)
    assert len(first_runner.calls) == 1
    primary_completion = next(
        workspace.glob("provider-operations/*/primary/completed.json")
    )
    primary_completion.unlink()

    response_lost_runner = _SequenceRunner([])
    restarted = CodexIdeaSkillAdapter(
        workspace,
        process_runner=response_lost_runner,
    )
    recovered_draft = restarted.generate_draft(primary_request)
    assert recovered_draft == draft
    assert response_lost_runner.calls == []

    review_request = replace(
        primary_request,
        native_session_ref=draft.primary_session_ref,
        job_ref="idea-review-operation:stable",
    )
    result = first.review_draft(review_request, draft)
    assert result.final_outcome == revised
    assert len(first_runner.calls) == 2
    next(workspace.glob("provider-operations/*/review/completed.json")).unlink()

    recovered_result = restarted.review_draft(review_request, recovered_draft)
    assert recovered_result == result
    assert response_lost_runner.calls == []


def test_durable_provider_operation_fails_closed_when_outcome_is_unknown(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "unknown-idea-provider"
    first_runner = _OutcomeUnknownRunner()
    first = CodexIdeaSkillAdapter(workspace, process_runner=first_runner)
    request = _request(
        runtime_binding=first.runtime_binding(),
        job_ref="idea-primary-operation:unknown",
    )

    with pytest.raises(IdeaSkillUnavailable, match="codex_cli_timeout"):
        first.generate_draft(request)
    assert first_runner.calls == 1

    forbidden_replay = _SequenceRunner([{"outcome": _idea_set()}])
    restarted = CodexIdeaSkillAdapter(
        workspace,
        process_runner=forbidden_replay,
    )
    with pytest.raises(
        IdeaSkillUnavailable,
        match="codex_operation_reconciliation_pending",
    ):
        restarted.generate_draft(request)
    assert forbidden_replay.calls == []


def test_durable_retry_uses_the_sealed_feature_snapshot_not_a_fresh_probe(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "volatile-feature-probe"
    first_runner = _FeatureProbeOutcomeUnknownRunner(None)
    first = CodexIdeaSkillAdapter(workspace, process_runner=first_runner)
    request = _request(
        runtime_binding=first.runtime_binding(),
        job_ref="idea-primary-operation:volatile-feature-probe",
    )

    with pytest.raises(IdeaSkillUnavailable, match="codex_cli_timeout"):
        first.generate_draft(request)

    recovered_probe = """\
hooks stable true
multi_agent stable true
plugins stable true
remote_plugin stable true
shell_tool stable true
skill_search stable true
unified_exec stable true
"""
    restarted_runner = _FeatureProbeOutcomeUnknownRunner(recovered_probe)
    restarted = CodexIdeaSkillAdapter(
        workspace,
        process_runner=restarted_runner,
    )
    with pytest.raises(
        IdeaSkillUnavailable,
        match="codex_operation_reconciliation_pending",
    ):
        restarted.generate_draft(request)

    assert restarted_runner.calls == 0


def test_oversized_feature_probe_is_diagnostic_only(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "oversized-feature-probe"
    runner = _OversizedFeatureProbeRunner([{"outcome": _idea_set()}])
    adapter = CodexIdeaSkillAdapter(workspace, process_runner=runner)
    request = _request(
        runtime_binding=adapter.runtime_binding(),
        job_ref="idea-primary-operation:oversized-feature-probe",
    )

    draft = adapter.generate_draft(request)

    assert draft.draft == _idea_set()
    _key_path, key = adapter._transport_key()
    invocation = read_transport_envelope(
        next(workspace.glob("provider-operations/*/primary/invocation.json")),
        key,
    )
    assert invocation["schema_ref"] == "meta-research/codex-provider-operation/v3"
    assert "root_capability_diagnostics" not in invocation


@pytest.mark.parametrize("tamper", ["mode", "delete_invocation"])
def test_unknown_operation_identity_tampering_cannot_authorize_replay(
    tmp_path: Path,
    tamper: str,
) -> None:
    workspace = tmp_path / f"unknown-identity-{tamper}"
    first_runner = _OutcomeUnknownRunner()
    first = CodexIdeaSkillAdapter(workspace, process_runner=first_runner)
    request = _request(
        runtime_binding=first.runtime_binding(),
        job_ref=f"idea-primary-operation:unknown-{tamper}",
    )
    with pytest.raises(IdeaSkillUnavailable, match="codex_cli_timeout"):
        first.generate_draft(request)
    operation = next(workspace.glob("provider-operations/*/primary"))
    invocation_path = operation / "invocation.json"
    if tamper == "mode":
        envelope = json.loads(invocation_path.read_text(encoding="utf-8"))
        envelope["payload"]["transport_mode"] = "durable_supervisor"
        invocation_path.write_text(
            json.dumps(envelope, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
    else:
        invocation_path.unlink()

    restarted = CodexIdeaSkillAdapter(workspace)
    with pytest.raises(
        IdeaSkillUnavailable,
        match="codex_operation_(identity_conflict|spool_invalid)",
    ):
        restarted.generate_draft(request)
    assert first_runner.calls == 1


def test_durable_provider_operation_never_promotes_a_failed_exit(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "failed-exit-idea-provider"
    failed_runner = _ExitFailureRunner()
    first = CodexIdeaSkillAdapter(workspace, process_runner=failed_runner)
    request = _request(
        runtime_binding=first.runtime_binding(),
        job_ref="idea-primary-operation:failed-exit",
    )

    with pytest.raises(
        IdeaSkillUnavailable, match="codex_operation_failed"
    ) as failed:
        first.generate_draft(request)
    assert failed.value.recovery_checkpoint is not None
    assert failed.value.recovery_checkpoint["termination_reason"] == "completed"
    assert failed_runner.calls == 1
    assert next(workspace.glob("provider-operations/*/primary/exit.json")).is_file()
    assert not list(workspace.glob("provider-operations/*/primary/completed.json"))

    forbidden_replay = _SequenceRunner([{"outcome": _idea_set()}])
    restarted = CodexIdeaSkillAdapter(
        workspace,
        process_runner=forbidden_replay,
    )
    with pytest.raises(
        IdeaSkillUnavailable, match="codex_operation_failed"
    ) as replayed:
        restarted.generate_draft(request)
    assert replayed.value.recovery_checkpoint == failed.value.recovery_checkpoint
    assert forbidden_replay.calls == []

    exit_path = next(workspace.glob("provider-operations/*/primary/exit.json"))
    tampered_exit = json.loads(exit_path.read_text(encoding="utf-8"))
    tampered_exit["returncode"] = 0
    exit_path.write_text(
        json.dumps(tampered_exit, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    with pytest.raises(
        IdeaSkillUnavailable,
        match="codex_operation_spool_invalid",
    ):
        restarted.generate_draft(request)
    assert forbidden_replay.calls == []


@pytest.mark.parametrize(
    "termination_reason,returncode",
    [
        ("descendant_process", 126),
        ("launch_failed", 127),
    ],
)
def test_every_signed_terminal_provider_exit_has_a_non_replayable_checkpoint(
    termination_reason: str,
    returncode: int,
) -> None:
    marker: dict[str, object] = {
        "termination_reason": termination_reason,
        "returncode": returncode,
        "invocation_hash": "1" * 64,
        "prompt_hash": "2" * 64,
        "output_schema_hash": "3" * 64,
        "stdout_hash": "4" * 64,
        "result_file_hash": None,
        "supervisor_receipt_hash": "5" * 64,
    }

    error = _provider_hard_ceiling_error(marker)

    assert error is not None
    assert error.code == "codex_operation_failed"
    assert error.recovery_checkpoint is not None
    assert error.recovery_checkpoint["termination_reason"] == termination_reason


def test_cooperative_stop_is_not_misclassified_as_a_provider_hard_ceiling() -> None:
    marker: dict[str, object] = {
        "termination_reason": "stopped",
        "returncode": 143,
        "invocation_hash": "1" * 64,
        "prompt_hash": "2" * 64,
        "output_schema_hash": "3" * 64,
        "stdout_hash": "4" * 64,
        "result_file_hash": None,
        "supervisor_receipt_hash": "5" * 64,
    }

    assert _provider_hard_ceiling_error(marker) is None


def test_durable_provider_seal_rejects_unsealed_result_tampering(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "tampered-result-idea-provider"
    runner = _SequenceRunner([{"outcome": _idea_set()}])
    first = CodexIdeaSkillAdapter(workspace, process_runner=runner)
    request = _request(
        runtime_binding=first.runtime_binding(),
        job_ref="idea-primary-operation:tampered-result",
    )
    first.generate_draft(request)
    operation = next(workspace.glob("provider-operations/*/primary"))
    (operation / "completed.json").unlink()
    (operation / "stdout.jsonl").write_text(
        json.dumps(
            {"type": "thread.started", "thread_id": "forged-native"}
        ),
        encoding="utf-8",
    )
    (operation / "last-message.json").write_text(
        json.dumps(
            {"outcome": _idea_set("伪造但结构合法的另一条干预方向")},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    forbidden_replay = _SequenceRunner([{"outcome": _idea_set()}])
    restarted = CodexIdeaSkillAdapter(
        workspace,
        process_runner=forbidden_replay,
    )
    with pytest.raises(
        IdeaSkillUnavailable,
        match="codex_operation_spool_invalid",
    ):
        restarted.generate_draft(request)
    assert forbidden_replay.calls == []
    assert not (operation / "completed.json").exists()


@pytest.mark.parametrize("filename", ["prompt.txt", "output-schema.json"])
def test_durable_provider_seal_binds_exact_invocation_inputs(
    tmp_path: Path,
    filename: str,
) -> None:
    workspace = tmp_path / f"tampered-input-{filename}"
    runner = _SequenceRunner([{"outcome": _idea_set()}])
    first = CodexIdeaSkillAdapter(workspace, process_runner=runner)
    request = _request(
        runtime_binding=first.runtime_binding(),
        job_ref=f"idea-primary-operation:tampered-{filename}",
    )
    first.generate_draft(request)
    operation = next(workspace.glob("provider-operations/*/primary"))
    (operation / filename).write_text("{}", encoding="utf-8")

    forbidden_replay = _SequenceRunner([{"outcome": _idea_set()}])
    restarted = CodexIdeaSkillAdapter(
        workspace,
        process_runner=forbidden_replay,
    )
    with pytest.raises(
        IdeaSkillUnavailable,
        match="codex_operation_spool_invalid",
    ):
        restarted.generate_draft(request)
    assert forbidden_replay.calls == []


def test_supervisor_recovers_a_child_result_after_daemon_response_loss(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "detached-supervisor-provider"
    executable = _fake_codex_executable(
        tmp_path / "fake-codex-supervised",
        read_all_input=True,
    )
    detached_runner = _DetachedSupervisorRunner()
    first = CodexIdeaSkillAdapter(
        workspace,
        executable=str(executable),
        process_runner=detached_runner,
    )
    request = _request(
        runtime_binding=first.runtime_binding(),
        job_ref="idea-primary-operation:detached-supervisor",
    )

    with pytest.raises(IdeaSkillUnavailable, match="codex_cli_io_unavailable"):
        first.generate_draft(request)
    assert detached_runner.process is not None
    assert detached_runner.process.wait(timeout=10) == 0

    forbidden_replay = _SequenceRunner([{"outcome": _idea_set()}])
    restarted = CodexIdeaSkillAdapter(
        workspace,
        executable=str(executable),
        process_runner=forbidden_replay,
    )
    recovered = restarted.generate_draft(request)
    assert recovered.primary_session_ref == "supervised-primary"
    assert recovered.draft == _idea_set()
    assert forbidden_replay.calls == []


def test_supervisor_rejects_partial_prompt_delivery(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "partial-prompt-provider"
    executable = _fake_codex_executable(
        tmp_path / "fake-codex-partial-prompt",
        read_all_input=False,
    )
    adapter = CodexIdeaSkillAdapter(workspace, executable=str(executable))
    request = _request(
        runtime_binding=adapter.runtime_binding(),
        job_ref="idea-primary-operation:partial-prompt",
    )

    with pytest.raises(
        IdeaSkillUnavailable,
        match="codex_operation_spool_invalid",
    ):
        adapter.generate_draft(request)

    operation = next(workspace.glob("provider-operations/*/primary"))
    receipt = json.loads(
        (operation / "supervisor-exit.json").read_text(encoding="utf-8")
    )
    assert receipt["payload"]["input_complete"] is False
    assert not (operation / "completed.json").exists()


def test_supervisor_owns_deadline_after_daemon_response_loss(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "supervisor-deadline-provider"
    executable = _fake_codex_executable(
        tmp_path / "fake-codex-supervisor-timeout",
        read_all_input=True,
        sleep_seconds=5,
    )
    detached_runner = _DetachedSupervisorRunner()
    first = CodexIdeaSkillAdapter(
        workspace,
        executable=str(executable),
        timeout_seconds=0.2,
        process_runner=detached_runner,
    )
    request = _request(
        runtime_binding=first.runtime_binding(),
        job_ref="idea-primary-operation:supervisor-timeout",
    )

    with pytest.raises(IdeaSkillUnavailable, match="codex_cli_io_unavailable"):
        first.generate_draft(request)
    assert detached_runner.process is not None
    assert detached_runner.process.wait(timeout=3) == 0
    operation = next(workspace.glob("provider-operations/*/primary"))
    receipt = json.loads(
        (operation / "supervisor-exit.json").read_text(encoding="utf-8")
    )
    assert receipt["payload"]["termination_reason"] == "timeout"
    assert receipt["payload"]["returncode"] != 0

    forbidden_replay = _SequenceRunner([{"outcome": _idea_set()}])
    restarted = CodexIdeaSkillAdapter(
        workspace,
        executable=str(executable),
        timeout_seconds=0.2,
        process_runner=forbidden_replay,
    )
    with pytest.raises(
        IdeaSkillUnavailable, match="codex_operation_timeout"
    ) as blocked:
        restarted.generate_draft(request)
    assert blocked.value.recovery_checkpoint is not None
    assert blocked.value.recovery_checkpoint["termination_reason"] == "timeout"
    assert blocked.value.recovery_checkpoint["supervisor_receipt_hash"]
    assert forbidden_replay.calls == []


def test_graceful_stop_leaves_a_signed_provider_termination(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "supervisor-stop-provider"
    executable = _fake_codex_executable(
        tmp_path / "fake-codex-supervisor-stop",
        read_all_input=True,
        sleep_seconds=30,
    )
    adapter = CodexIdeaSkillAdapter(
        workspace,
        executable=str(executable),
        timeout_seconds=60,
    )
    request = _request(
        runtime_binding=adapter.runtime_binding(),
        job_ref="idea-primary-operation:supervisor-stop",
    )
    errors: list[BaseException] = []

    def invoke() -> None:
        try:
            adapter.generate_draft(request)
        except BaseException as error:
            errors.append(error)

    worker = threading.Thread(target=invoke)
    worker.start()
    deadline = time.monotonic() + 5
    while not list(workspace.glob("provider-operations/*/primary/pid.json")):
        if time.monotonic() >= deadline:
            raise AssertionError("supervisor did not start")
        time.sleep(0.01)
    adapter.request_stop()
    worker.join(timeout=10)
    assert not worker.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], IdeaSkillUnavailable)
    assert errors[0].code == "codex_cli_stopped"

    operation = next(workspace.glob("provider-operations/*/primary"))
    receipt = json.loads(
        (operation / "supervisor-exit.json").read_text(encoding="utf-8")
    )
    assert receipt["payload"]["termination_reason"] == "stopped"

    forbidden_replay = _SequenceRunner([{"outcome": _idea_set()}])
    restarted = CodexIdeaSkillAdapter(
        workspace,
        executable=str(executable),
        timeout_seconds=60,
        process_runner=forbidden_replay,
    )
    with pytest.raises(IdeaSkillUnavailable, match="codex_operation_failed"):
        restarted.generate_draft(request)
    assert forbidden_replay.calls == []


def test_cleanup_terminates_provider_orphaned_by_supervisor_crash(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "orphaned-supervisor-provider"
    executable = _fake_codex_executable(
        tmp_path / "fake-codex-orphaned-supervisor",
        read_all_input=True,
        sleep_seconds=30,
    )
    runner = _DetachedSupervisorRunner()
    adapter = CodexIdeaSkillAdapter(
        workspace,
        executable=str(executable),
        timeout_seconds=60,
        process_runner=runner,
    )
    job_ref = "idea-primary-operation:orphaned-supervisor"
    request = _request(
        runtime_binding=adapter.runtime_binding(),
        job_ref=job_ref,
    )

    with pytest.raises(IdeaSkillUnavailable, match="codex_cli_io_unavailable"):
        adapter.generate_draft(request)
    operation = next(workspace.glob("provider-operations/*/primary"))
    deadline = time.monotonic() + 5
    while not (operation / "provider-started.json").is_file():
        if time.monotonic() >= deadline:
            raise AssertionError("provider process marker was not published")
        time.sleep(0.01)
    marker = json.loads(
        (operation / "provider-started.json").read_text(encoding="utf-8")
    )["payload"]
    provider_process_id = int(marker["provider_process_id"])
    assert runner.process is not None
    os.kill(runner.process.pid, signal.SIGKILL)
    runner.process.wait(timeout=2)
    assert not (operation / "supervisor-exit.json").exists()

    assert adapter.reconcile_cancelled_job(job_ref) is True
    deadline = time.monotonic() + 2
    while Path(f"/proc/{provider_process_id}").exists():
        if time.monotonic() >= deadline:
            raise AssertionError("orphaned provider survived cleanup reconciliation")
        time.sleep(0.01)
    assert not (operation / "supervisor-exit.json").exists()


def test_durable_job_cancel_survives_daemon_runner_replacement(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "supervisor-durable-cancel-provider"
    executable = _fake_codex_executable(
        tmp_path / "fake-codex-supervisor-durable-cancel",
        read_all_input=True,
        sleep_seconds=5,
    )
    job_ref = "idea-primary-operation:durable-cancel"
    first = CodexIdeaSkillAdapter(
        workspace,
        executable=str(executable),
        timeout_seconds=10,
    )
    request = _request(runtime_binding=first.runtime_binding(), job_ref=job_ref)
    errors: list[BaseException] = []

    def invoke() -> None:
        try:
            first.generate_draft(request)
        except BaseException as error:
            errors.append(error)

    worker = threading.Thread(target=invoke)
    worker.start()
    deadline = time.monotonic() + 5
    while not list(workspace.glob("provider-operations/*/primary/pid.json")):
        if time.monotonic() >= deadline:
            raise AssertionError("supervisor did not start")
        time.sleep(0.01)

    # This adapter has a fresh in-memory runner, as it would after daemon restart.
    restarted = CodexIdeaSkillAdapter(
        workspace,
        executable=str(executable),
        timeout_seconds=10,
    )
    restarted.cancel_job(job_ref)

    worker.join(timeout=5)
    assert not worker.is_alive()
    assert len(errors) == 1
    operation = next(workspace.glob("provider-operations/*/primary"))
    receipt = json.loads(
        (operation / "supervisor-exit.json").read_text(encoding="utf-8")
    )
    assert receipt["payload"]["termination_reason"] == "stopped"


def test_durable_supervisor_recovers_a_prelaunch_daemon_loss(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "prelaunch-recovery-provider"
    executable = _fake_codex_executable(
        tmp_path / "fake-codex-prelaunch",
        read_all_input=True,
    )
    first = CodexIdeaSkillAdapter(
        workspace,
        executable=str(executable),
        process_runner=_PrelaunchLossRunner(),
    )
    request = _request(
        runtime_binding=first.runtime_binding(),
        job_ref="idea-primary-operation:prelaunch-loss",
    )

    with pytest.raises(IdeaSkillUnavailable, match="codex_cli_io_unavailable"):
        first.generate_draft(request)
    operation = next(workspace.glob("provider-operations/*/primary"))
    assert (operation / "supervisor-request.json").is_file()
    assert not (operation / "provider-started.json").exists()
    _key_path, key = first._transport_key()
    supervisor_request = read_transport_envelope(
        operation / "supervisor-request.json", key
    )
    assert supervisor_request["schema_ref"] == CODEX_SUPERVISOR_REQUEST_SCHEMA_V2
    assert supervisor_request["prompt_max_bytes"] == PROVIDER_STREAM_MAX_BYTES
    assert supervisor_request["stream_max_bytes"] == PROVIDER_STREAM_MAX_BYTES
    assert supervisor_request["result_max_bytes"] == PROVIDER_RESULT_MAX_BYTES

    restarted = CodexIdeaSkillAdapter(workspace, executable=str(executable))
    recovered = restarted.generate_draft(request)

    assert recovered.primary_session_ref == "supervised-primary"
    assert recovered.draft == _idea_set()
    assert (operation / "provider-started.json").is_file()
    assert (operation / "completed.json").is_file()


@pytest.mark.parametrize(
    "legacy_schema",
    (
        "meta-research/codex-provider-operation/v1",
        "meta-research/codex-provider-operation/v2",
    ),
)
def test_durable_supervisor_rejects_old_operation_schemas(
    tmp_path: Path, legacy_schema: str
) -> None:
    workspace = tmp_path / "old-operation-schema-provider"
    executable = _fake_codex_executable(
        tmp_path / "fake-codex-old-operation-schema",
        read_all_input=True,
    )
    first = CodexIdeaSkillAdapter(
        workspace,
        executable=str(executable),
        process_runner=_PrelaunchLossRunner(),
    )
    request = _request(
        runtime_binding=first.runtime_binding(),
        job_ref="idea-primary-operation:old-operation-schema",
    )

    with pytest.raises(IdeaSkillUnavailable, match="codex_cli_io_unavailable"):
        first.generate_draft(request)
    operation = next(workspace.glob("provider-operations/*/primary"))
    _key_path, key = first._transport_key()
    invocation_path = operation / "invocation.json"
    invocation = read_transport_envelope(invocation_path, key)
    invocation["schema_ref"] = legacy_schema
    invocation_path.unlink()
    write_transport_envelope(invocation_path, invocation, key)

    restarted = CodexIdeaSkillAdapter(workspace, executable=str(executable))
    with pytest.raises(
        IdeaSkillUnavailable, match="codex_operation_spool_invalid"
    ):
        restarted.generate_draft(request)
    assert not (operation / "provider-started.json").exists()
    assert not (operation / "completed.json").exists()


def test_cancelled_prelaunch_operation_becomes_safe_after_startup_window(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "prelaunch-cancel-provider"
    executable = _fake_codex_executable(
        tmp_path / "fake-codex-prelaunch-cancel",
        read_all_input=True,
    )
    adapter = CodexIdeaSkillAdapter(
        workspace,
        executable=str(executable),
        process_runner=_PrelaunchLossRunner(),
    )
    job_ref = "idea-primary-operation:prelaunch-cancel"
    request = _request(
        runtime_binding=adapter.runtime_binding(),
        job_ref=job_ref,
    )

    with pytest.raises(IdeaSkillUnavailable, match="codex_cli_io_unavailable"):
        adapter.generate_draft(request)
    operation = next(workspace.glob("provider-operations/*/primary"))
    request_path = operation / "supervisor-request.json"
    assert request_path.is_file()
    assert adapter.reconcile_cancelled_job(job_ref) is False

    # Simulate a restart after the runner's persisted five-second launch window.
    os.utime(request_path, (0, 0))
    assert adapter.reconcile_cancelled_job(job_ref) is True
    assert not (operation / "supervisor-ready.json").exists()
    assert not (operation / "provider-started.json").exists()
    assert not (operation / "supervisor-exit.json").exists()


def test_adapter_rejects_multiple_native_session_identities(
    tmp_path: Path,
) -> None:
    runner = _MultipleNativeSessionRunner([{"outcome": _idea_set()}])
    adapter = CodexIdeaSkillAdapter(
        tmp_path / "multiple-native-sessions",
        process_runner=runner,
    )
    request = _request(runtime_binding=adapter.runtime_binding())

    with pytest.raises(
        IdeaSkillUnavailable,
        match="codex_native_session_mismatch",
    ):
        adapter.generate_draft(request)


def test_supervisor_terminates_provider_descendants_before_sealing(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "provider-descendant"
    child_pid_path = tmp_path / "descendant.pid"
    executable = _descendant_codex_executable(
        tmp_path / "fake-codex-descendant",
        child_pid_path,
    )
    adapter = CodexIdeaSkillAdapter(workspace, executable=str(executable))
    request = _request(
        runtime_binding=adapter.runtime_binding(),
        job_ref="idea-primary-operation:descendant",
    )

    with pytest.raises(
        IdeaSkillUnavailable, match="codex_operation_failed"
    ) as failed:
        adapter.generate_draft(request)
    assert failed.value.recovery_checkpoint is not None
    assert (
        failed.value.recovery_checkpoint["termination_reason"]
        == "descendant_process"
    )

    operation = next(workspace.glob("provider-operations/*/primary"))
    receipt = json.loads(
        (operation / "supervisor-exit.json").read_text(encoding="utf-8")
    )
    assert receipt["payload"]["termination_reason"] == "descendant_process"
    child_pid = int(child_pid_path.read_text(encoding="utf-8"))
    deadline = time.monotonic() + 2
    while True:
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        if time.monotonic() >= deadline:
            raise AssertionError("provider descendant survived supervisor seal")
        time.sleep(0.01)


def test_result_limit_does_not_limit_native_session_files(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "native-session-file-budget"
    session_file = tmp_path / "native-session.jsonl"
    executable = tmp_path / "fake-codex-large-native-session"
    encoded_outcome = repr(json.dumps({"outcome": _idea_set()}, ensure_ascii=False))
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        "from pathlib import Path\n"
        "import sys\n"
        "if '--version' in sys.argv:\n"
        "    print('codex-native-session-budget-test 1')\n"
        "    raise SystemExit(0)\n"
        "sys.stdin.buffer.read()\n"
        f"Path({str(session_file)!r}).write_bytes(b'x' * "
        f"({PROVIDER_RESULT_MAX_BYTES} * 2))\n"
        "args = sys.argv[1:]\n"
        "result_path = Path(args[args.index('--output-last-message') + 1])\n"
        f"result_path.write_text({encoded_outcome}, encoding='utf-8')\n"
        "print(json.dumps({'type': 'thread.started', "
        "'thread_id': 'large-session-primary'}))\n",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    adapter = CodexIdeaSkillAdapter(workspace, executable=str(executable))
    request = _request(
        runtime_binding=adapter.runtime_binding(),
        job_ref="idea-primary-operation:large-native-session",
    )

    draft = adapter.generate_draft(request)

    assert draft.primary_session_ref == "large-session-primary"
    assert session_file.stat().st_size == PROVIDER_RESULT_MAX_BYTES * 2
