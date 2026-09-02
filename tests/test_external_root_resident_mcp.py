from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import threading
from types import SimpleNamespace
from unittest.mock import Mock

from meta_research.acquisition import (
    AcquisitionBatchRequest,
    AcquisitionItemResult,
    AcquisitionPaper,
    AcquisitionPreflightResult,
    AcquisitionRuntimeBinding,
)
from meta_research.acquisition_root import CodexAcquisitionRootAdapter
from meta_research.companion import CodexCompanionAdapter
from meta_research.composition import build_production_runtime
from meta_research.owners.common import canonical_hash
from meta_research.paths import prepare_data_root
from meta_research.power_inhibitors import OperatorAttestedPowerInhibitor
from meta_research.quest_drafting import (
    HostComputeDevice,
    HostComputeSnapshot,
    IntentTurnResult,
    ProposalDraftResult,
)
from meta_research.root_resident_mcp import RootResidentMcpChannels
from meta_research.semantic_owner_gateway import (
    ROOT_AGENT_SEMANTIC_OPERATION_IDS,
)


class _SequenceRunner:
    def __init__(self, outputs: list[dict[str, object]]) -> None:
        self._outputs = iter(outputs)
        self.environments: list[dict[str, str] | None] = []

    def __call__(
        self,
        argv: list[str],
        prompt: str,
        timeout: float | None,
        environment: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        del prompt, timeout
        self.environments.append(environment)
        output_path = Path(argv[argv.index("--output-last-message") + 1])
        output_path.write_text(
            json.dumps(next(self._outputs), ensure_ascii=False),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout=json.dumps(
                {"type": "thread.started", "thread_id": "root-session:1"}
            ),
            stderr="",
        )

    def run_job(
        self,
        job_ref: str,
        argv: list[str],
        prompt: str,
        timeout: float | None,
        environment: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        del job_ref
        return self(argv, prompt, timeout, environment)


class _AcquisitionDelegate:
    def __init__(self) -> None:
        self.batches: list[AcquisitionBatchRequest] = []

    def runtime_binding(self) -> AcquisitionRuntimeBinding:
        return AcquisitionRuntimeBinding(
            provider_ref="test/acquisition-delegate",
            provider_version="1",
            capability_bindings=(
                "browser-context-reuse",
                "lawful-fulltext-routing",
                "private-manifest",
            ),
        )

    def preflight(self, request: object) -> AcquisitionPreflightResult:
        del request
        return AcquisitionPreflightResult(
            status="ready",
            browser_context_ref=None,
            reason_code=None,
            evidence={},
        )

    def acquire(
        self, request: AcquisitionBatchRequest
    ) -> tuple[AcquisitionItemResult, ...]:
        self.batches.append(request)
        return (
            AcquisitionItemResult(
                paper_id="paper:1",
                status="missing",
                path=None,
                format=None,
                failure={
                    "code": "oa_fulltext_not_found",
                    "detail": "No lawful full text was found.",
                },
            ),
        )

    def request_stop(self) -> None:
        return None


class _QuestDrafter:
    def draft(self, request: object) -> ProposalDraftResult:
        del request
        return ProposalDraftResult(
            content={
                "title": "Resident MCP acquisition",
                "unknown_statement": "Which source remains unavailable?",
                "answer_shape": "A bounded source availability result.",
                "applicability_scope": "This Quest.",
                "background_context": "",
                "requirements_constraints": "",
            },
            adapter_kind="test",
        )

    def reply(self, request: object) -> IntentTurnResult:
        del request
        return IntentTurnResult(
            reply="test",
            native_session_ref="quest-drafter-session",
            adapter_kind="test",
        )


class _HostProbe:
    def observe(self) -> HostComputeSnapshot:
        return HostComputeSnapshot(
            status="ready",
            observed_at=1_800_000_000.0,
            devices=(
                HostComputeDevice(
                    uuid="GPU-resident-acquisition",
                    name="Resident acquisition test GPU",
                    memory_total_mib=24_576,
                ),
            ),
            adapter_kind="test",
        )


def _fake_codex(path: Path) -> Path:
    path.write_text("#!/bin/sh\nprintf 'codex-test 1\\n'\n", encoding="utf-8")
    path.chmod(0o700)
    return path


def _scope(root_kind: str) -> dict[str, object]:
    return {
        "quest_ref": f"quest:{root_kind}",
        "run_ref": f"{root_kind}-run:1",
        "attempt_ref": f"{root_kind}-attempt:1",
        "root_session_ref": f"{root_kind}-root-session:1",
        "fence_ref": f"{root_kind}-fence:1",
        "runtime_binding_hash": "7" * 64,
        "generation": 1,
    }


def _authority(root_kind: str, phase: str) -> Mock:
    operation_ids = ROOT_AGENT_SEMANTIC_OPERATION_IDS[root_kind]
    operation_bindings = tuple(
        {"semantic_operation_id": operation_id}
        for operation_id in operation_ids
    )
    authority = Mock()
    authority.require_operation_binding.return_value = SimpleNamespace(
        contract_ref="meta-research/harness-operation-binding/v1",
        contract_hash="5" * 64,
        conformance_ref=f"operation-binding:{root_kind}",
        semantic_mcp_catalog_hash="6" * 64,
        semantic_mcp_operation_bindings_hash=canonical_hash(
            list(operation_bindings)
        ),
        required_families=("codex",),
        required_capabilities=("semantic_mcp",),
        required_operation_ids=operation_ids,
        profile_receipts=(),
        as_dict=lambda: {"root_kind": root_kind, "catalog": "6" * 64},
    )
    authority.issue_resident_mcp_channel.return_value = SimpleNamespace(
        connection=SimpleNamespace(
            token=f"{root_kind}-token", grant_ref=f"{root_kind}-grant"
        ),
        binding=SimpleNamespace(
            endpoint_ref="/mcp",
            catalog_hash="6" * 64,
            connection_grant_ref=f"{root_kind}-grant",
            operation_bindings=operation_bindings,
            root_kind=root_kind,
            phase=phase,
        ),
    )
    return authority


def test_concurrent_exact_root_channel_is_issued_and_revoked_once() -> None:
    channels = RootResidentMcpChannels("idea")
    authority = _authority("idea", "primary")
    issued = threading.Event()
    allow_issue = threading.Event()
    channel = authority.issue_resident_mcp_channel.return_value

    def issue_once(**values: object) -> object:
        del values
        issued.set()
        assert allow_issue.wait(1)
        return channel

    authority.issue_resident_mcp_channel.side_effect = issue_once
    channels.bind_authority(authority)
    channels.configure_endpoint("http://127.0.0.1:8766")
    scope = _scope("idea")

    def acquire() -> tuple[tuple[str, str, str], object]:
        return channels.acquire(
            run_ref=str(scope["run_ref"]),
            attempt_ref=str(scope["attempt_ref"]),
            root_session_ref=str(scope["root_session_ref"]),
            fence_ref=str(scope["fence_ref"]),
            capability_binding_hash=str(scope["runtime_binding_hash"]),
            phase="primary",
            job_ref="idea-job:concurrent",
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(acquire)
        assert issued.wait(1)
        second = pool.submit(acquire)
        allow_issue.set()
        first_key, first_access = first.result(timeout=1)
        second_key, second_access = second.result(timeout=1)

    assert first_key == second_key
    assert first_access.token == second_access.token
    authority.issue_resident_mcp_channel.assert_called_once()
    channels.release(first_key)
    authority.revoke_resident_mcp_channel.assert_not_called()
    channels.release(second_key)
    authority.revoke_resident_mcp_channel.assert_called_once_with(channel)


def test_quest_bound_acquisition_batch_uses_exact_resident_operation_tree(
    tmp_path: Path,
) -> None:
    runner = _SequenceRunner([{"accepted": True, "human_request": None}])
    adapter = CodexAcquisitionRootAdapter(
        tmp_path / "acquisition",
        _AcquisitionDelegate(),  # type: ignore[arg-type]
        executable=str(_fake_codex(tmp_path / "acquisition-codex")),
        process_runner=runner,
    )
    authority = _authority("acquisition", "acquisition-root-turn")
    adapter.bind_resident_mcp_authority(authority)
    adapter.configure_resident_mcp_endpoint("http://127.0.0.1:8766")
    scope = _scope("acquisition")
    request = SimpleNamespace(
        session_ref="acquisition-session:1",
        request_id="acquisition-request:1",
        root_runtime_scope=scope,
    )

    result = adapter.acquire(request)  # type: ignore[arg-type]

    assert result[0].status == "missing"
    issued = authority.issue_resident_mcp_channel.call_args.kwargs
    assert issued["subject_policy"] == "operation_tree"
    assert issued["root_kind"] == "acquisition"
    assert issued["run_ref"] == scope["run_ref"]
    assert issued["operation_ids"] == ROOT_AGENT_SEMANTIC_OPERATION_IDS[
        "acquisition"
    ]
    authority.revoke_resident_mcp_channel.assert_called_once()
    assert runner.environments == [
        {
            "META_RESEARCH_MCP_TOKEN": "acquisition-token",
            "NO_PROXY": "127.0.0.1,localhost,::1",
            "no_proxy": "127.0.0.1,localhost,::1",
        }
    ]


def test_unmanaged_acquisition_turn_remains_without_resident_channel(
    tmp_path: Path,
) -> None:
    runner = _SequenceRunner([{"accepted": True, "human_request": None}])
    adapter = CodexAcquisitionRootAdapter(
        tmp_path / "unmanaged-acquisition",
        _AcquisitionDelegate(),  # type: ignore[arg-type]
        executable=str(_fake_codex(tmp_path / "unmanaged-acquisition-codex")),
        process_runner=runner,
    )
    authority = _authority("acquisition", "acquisition-root-turn")
    adapter.bind_resident_mcp_authority(authority)
    adapter.configure_resident_mcp_endpoint("http://127.0.0.1:8766")
    request = SimpleNamespace(
        session_ref="pre-quest-acquisition-session:1",
        config_hash="9" * 64,
    )

    adapter.preflight(request)  # type: ignore[arg-type]

    authority.issue_resident_mcp_channel.assert_not_called()
    assert runner.environments == [None]


def test_quest_bound_companion_turn_uses_exact_resident_operation_tree(
    tmp_path: Path,
) -> None:
    runner = _SequenceRunner([{"reply": "继续核对证据边界。"}])
    adapter = CodexCompanionAdapter(
        tmp_path / "companion",
        executable=str(_fake_codex(tmp_path / "companion-codex")),
        process_runner=runner,
    )
    authority = _authority("companion", "companion-turn")
    adapter.bind_resident_mcp_authority(authority)
    adapter.configure_resident_mcp_endpoint("http://127.0.0.1:8766")
    scope = _scope("companion")
    request = SimpleNamespace(
        initialization_id="quest:companion",
        draft_revision=1,
        draft_hash="a" * 64,
        draft={"interaction_kind": "quest"},
        message="下一步是什么？",
        native_session_ref=None,
        job_ref="companion-job:1",
        creation_context_kind="quest_initialization",
        creation_context_ref=None,
        context_generation=None,
        root_runtime_scope=scope,
    )

    result = adapter.reply(request)  # type: ignore[arg-type]

    assert result.reply == "继续核对证据边界。"
    issued = authority.issue_resident_mcp_channel.call_args.kwargs
    assert issued["subject_policy"] == "operation_tree"
    assert issued["root_kind"] == "companion"
    assert issued["run_ref"] == scope["run_ref"]
    assert issued["operation_ids"] == ROOT_AGENT_SEMANTIC_OPERATION_IDS[
        "companion"
    ]
    authority.revoke_resident_mcp_channel.assert_called_once()
    assert runner.environments[0] is not None
    assert runner.environments[0]["META_RESEARCH_MCP_TOKEN"] == "companion-token"


def test_pre_quest_companion_turn_remains_without_resident_channel(
    tmp_path: Path,
) -> None:
    runner = _SequenceRunner([{"reply": "先澄清研究目标。"}])
    adapter = CodexCompanionAdapter(
        tmp_path / "pre-quest-companion",
        executable=str(_fake_codex(tmp_path / "pre-quest-companion-codex")),
        process_runner=runner,
    )
    authority = _authority("companion", "companion-turn")
    adapter.bind_resident_mcp_authority(authority)
    adapter.configure_resident_mcp_endpoint("http://127.0.0.1:8766")
    request = SimpleNamespace(
        initialization_id="initialization:pre-quest",
        draft_revision=1,
        draft_hash="b" * 64,
        draft={},
        message="研究什么？",
        native_session_ref=None,
        job_ref="pre-quest-companion-job:1",
        creation_context_kind="quest_initialization",
        creation_context_ref=None,
        context_generation=None,
    )

    adapter.reply(request)  # type: ignore[arg-type]

    authority.issue_resident_mcp_channel.assert_not_called()
    assert runner.environments == [None]


def test_production_acquisition_owner_injects_scope_into_actual_root_adapter(
    tmp_path: Path,
) -> None:
    delegate = _AcquisitionDelegate()
    runner = _SequenceRunner(
        [
            {"accepted": True, "human_request": None},
            {"accepted": True, "human_request": None},
        ]
    )
    adapter = CodexAcquisitionRootAdapter(
        tmp_path / "production-acquisition-provider",
        delegate,  # type: ignore[arg-type]
        executable=str(_fake_codex(tmp_path / "production-acquisition-codex")),
        process_runner=runner,
    )
    quest_drafter = _QuestDrafter()
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "production-acquisition-data"),
        acquisition_provider=adapter,
        proposal_drafter=quest_drafter,
        intent_drafting_provider=quest_drafter,
        host_compute_probe=_HostProbe(),
        power_inhibitor=OperatorAttestedPowerInhibitor(),
        startup_power_probe=False,
        startup_harness_diagnostics=False,
    )
    issued_scopes: list[dict[str, object]] = []
    issue_channel = runtime.harnesses.issue_resident_mcp_channel

    def record_issue(**scope: object):
        issued_scopes.append(dict(scope))
        return issue_channel(**scope)  # type: ignore[arg-type]

    runtime.harnesses.issue_resident_mcp_channel = record_issue  # type: ignore[method-assign]
    try:
        runtime.configure_resident_mcp_endpoint("http://127.0.0.1:8766")
        human = runtime.owners.human_collaboration
        opened = human.create_quest({}, "resident-acquisition-open")
        initialization_id = opened["initialization_id"]
        probed = human.observe_host_compute(
            initialization_id,
            ["GPU-resident-acquisition"],
            "resident-acquisition-compute",
        )
        draft = dict(probed["quest_draft"]["value"])
        draft.update(
            {
                "goal": "Check one bounded source.",
                "completion_criteria": "Record its availability.",
                "time_budget": "30d",
                "route": "direct",
                "literature": {
                    "mode": "oa_only",
                    "library_entry_url": "",
                    "scope_exclusions": "",
                    "accepted_material_bindings": [],
                },
                "background_and_initial_direction": "Start with public sources.",
            }
        )
        revised = human.revise_quest_draft(
            initialization_id,
            draft,
            probed["quest_draft"]["hash"],
            "resident-acquisition-draft",
            probed["quest_draft"]["revision"],
        )
        session = runtime.owners.agent_runtime.prepare_acquisition_session(
            initialization_id=initialization_id,
            draft_revision=revised["quest_draft"]["revision"],
            config={"mode": "oa_only", "library_entry_url": ""},
            provider=adapter,
        )
        assert session.status == "ready"
        human.generate_question_proposal(
            initialization_id,
            revised["quest_draft"]["hash"],
            "resident-acquisition-proposal",
            revised["quest_draft"]["revision"],
        )
        assert human.process_drafting_once()
        proposed = human.query_quest_creation(initialization_id)
        previewed = human.preview_confirmation(
            initialization_id,
            quest_draft_revision=proposed["quest_draft"]["revision"],
            quest_draft_hash=proposed["quest_draft"]["hash"],
            proposal_ref=proposed["proposal"]["ref"],
            proposal_hash=proposed["proposal"]["hash"],
            idempotency_key="resident-acquisition-preview",
        )
        human.confirm_quest(
            initialization_id,
            quest_draft_revision=proposed["quest_draft"]["revision"],
            quest_draft_hash=proposed["quest_draft"]["hash"],
            proposal_ref=proposed["proposal"]["ref"],
            proposal_hash=proposed["proposal"]["hash"],
            preview_ref=previewed["confirmation_preview"]["ref"],
            preview_hash=previewed["confirmation_preview"]["hash"],
            idempotency_key="resident-acquisition-confirm",
        )
        assert human.reconcile_once()
        creation = human.query_quest_creation(initialization_id)
        quest_ref = creation["quest_ref"]
        bound_session = (
            runtime.owners.agent_runtime.bind_acquisition_session_to_quest(
                initialization_id, quest_ref
            )
        )
        assert bound_session is not None
        assert bound_session.quest_ref == quest_ref

        execution = runtime.owners.agent_runtime.acquire_literature(
            session.session_ref,
            AcquisitionBatchRequest(
                request_id="resident-acquisition-request",
                route_policy="oa_first_then_institution",
                papers=(
                    AcquisitionPaper(
                        paper_id="paper:1",
                        title="A missing paper",
                        doi=None,
                        arxiv_id=None,
                        source_urls=(),
                    ),
                ),
            ),
            adapter,
        )

        assert len(delegate.batches) == 1
        root_scope = delegate.batches[0].root_runtime_scope
        assert root_scope is not None
        assert execution.status == "missing"
        assert root_scope["quest_ref"] == quest_ref
        assert issued_scopes == [
            {
                "root_kind": "acquisition",
                "phase": "acquisition-root-turn",
                "subject_policy": "operation_tree",
                "run_ref": root_scope["run_ref"],
                "attempt_ref": root_scope["attempt_ref"],
                "root_session_ref": root_scope["root_session_ref"],
                "fence_ref": root_scope["fence_ref"],
                "capability_binding_hash": root_scope[
                    "runtime_binding_hash"
                ],
                "operation_ids": ROOT_AGENT_SEMANTIC_OPERATION_IDS[
                    "acquisition"
                ],
            }
        ]
        completed = runtime.owners.agent_runtime.query_managed_run(
            root_scope["run_ref"]
        )
        assert completed is not None and completed["status"] == "completed"
        assert runner.environments[0] is None
        assert runner.environments[1] is not None
        assert "META_RESEARCH_MCP_TOKEN" in runner.environments[1]
    finally:
        runtime.close()


def test_production_companion_owner_injects_scope_into_actual_root_adapter(
    tmp_path: Path,
) -> None:
    runner = _SequenceRunner(
        [{"reply": "A bounded companion answer.", "agent_proposal": None}]
    )
    adapter = CodexCompanionAdapter(
        tmp_path / "production-companion-provider",
        executable=str(_fake_codex(tmp_path / "production-companion-codex")),
        process_runner=runner,
    )
    quest_drafter = _QuestDrafter()
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "production-companion-data"),
        proposal_drafter=quest_drafter,
        intent_drafting_provider=adapter,
        host_compute_probe=_HostProbe(),
        power_inhibitor=OperatorAttestedPowerInhibitor(),
        startup_power_probe=False,
        startup_harness_diagnostics=False,
    )
    issued_scopes: list[dict[str, object]] = []
    issue_channel = runtime.harnesses.issue_resident_mcp_channel

    def record_issue(**scope: object):
        issued_scopes.append(dict(scope))
        return issue_channel(**scope)  # type: ignore[arg-type]

    runtime.harnesses.issue_resident_mcp_channel = record_issue  # type: ignore[method-assign]
    try:
        runtime.configure_resident_mcp_endpoint("http://127.0.0.1:8766")
        human = runtime.owners.human_collaboration
        opened = human.create_quest({}, "resident-companion-open")
        initialization_id = opened["initialization_id"]
        probed = human.observe_host_compute(
            initialization_id,
            ["GPU-resident-acquisition"],
            "resident-companion-compute",
        )
        draft = dict(probed["quest_draft"]["value"])
        draft.update(
            {
                "goal": "Discuss one bounded source.",
                "completion_criteria": "Record a bounded answer.",
                "time_budget": "30d",
                "route": "direct",
                "literature": {
                    "mode": "oa_only",
                    "library_entry_url": "",
                    "scope_exclusions": "",
                    "accepted_material_bindings": [],
                },
                "background_and_initial_direction": "Start with public sources.",
            }
        )
        revised = human.revise_quest_draft(
            initialization_id,
            draft,
            probed["quest_draft"]["hash"],
            "resident-companion-draft",
            probed["quest_draft"]["revision"],
        )
        human.generate_question_proposal(
            initialization_id,
            revised["quest_draft"]["hash"],
            "resident-companion-proposal",
            revised["quest_draft"]["revision"],
        )
        assert human.process_drafting_once()
        proposed = human.query_quest_creation(initialization_id)
        previewed = human.preview_confirmation(
            initialization_id,
            quest_draft_revision=proposed["quest_draft"]["revision"],
            quest_draft_hash=proposed["quest_draft"]["hash"],
            proposal_ref=proposed["proposal"]["ref"],
            proposal_hash=proposed["proposal"]["hash"],
            idempotency_key="resident-companion-preview",
        )
        human.confirm_quest(
            initialization_id,
            quest_draft_revision=proposed["quest_draft"]["revision"],
            quest_draft_hash=proposed["quest_draft"]["hash"],
            proposal_ref=proposed["proposal"]["ref"],
            proposal_hash=proposed["proposal"]["hash"],
            preview_ref=previewed["confirmation_preview"]["ref"],
            preview_hash=previewed["confirmation_preview"]["hash"],
            idempotency_key="resident-companion-confirm",
        )
        assert human.reconcile_once()
        quest_ref = human.query_quest_creation(initialization_id)["quest_ref"]
        scope_ref = f"quest:{quest_ref}"
        queued = human.send_companion_message(
            scope_ref,
            "Give one bounded answer.",
            "resident-companion-message",
        )

        assert human.process_drafting_once()
        assert len(issued_scopes) == 1
        issued = issued_scopes[0]
        assert issued["root_kind"] == "companion"
        assert issued["phase"] == "companion-turn"
        assert issued["subject_policy"] == "operation_tree"
        assert issued["run_ref"] == queued["interaction_ref"]
        assert issued["operation_ids"] == ROOT_AGENT_SEMANTIC_OPERATION_IDS[
            "companion"
        ]
        completed = runtime.owners.agent_runtime.query_managed_run(
            issued["run_ref"]
        )
        assert completed is not None and completed["status"] == "completed"
        [turn] = human.query_companion(scope_ref)["turns"]
        assert turn["assistant_status"] == "completed"
        assert turn["assistant_content"] == "A bounded companion answer."
        assert runner.environments[0] is not None
        assert "META_RESEARCH_MCP_TOKEN" in runner.environments[0]
    finally:
        runtime.close()
