from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

from meta_research.acquisition import (
    AcquisitionBatchExecution,
    AcquisitionBatchRequest,
    AcquisitionItemResult,
)
from meta_research.deepfetch import (
    CodexDeepFetchAdapter,
    DeepFetchProviderRequest,
    DeepFetchResult,
    DeepFetchRuntimeBinding,
    DeepFetchUnavailable,
    validate_deepfetch_result,
)
from meta_research.owners.common import AcceptanceReceipt

RESULT = {
    "completion": "limited",
    "summary": "检索到一篇可核查论文，但全文不可用。",
    "papers": [
        {
            "title": "A verifiable paper",
            "url": "https://example.org/paper",
            "doi": None,
            "source_kind": "publisher",
            "fulltext_status": "unavailable",
            "retrieved_at": "2026-08-22T00:00:00Z",
        }
    ],
    "fulltexts": [],
    "limitations": ["没有可合法获取的开放全文。"],
}

PROTOTYPE_COMMIT = "cb369c938da835bcd07202e03ccc770551984070"


def _empty_reading(*, status: str = "not_read") -> dict[str, object]:
    return {
        "status": status,
        "understanding_summary": (
            "全文支持这项方法在指定任务上的主要实验结论。"
            if status == "complete"
            else None
        ),
        "methods": ["verified method"] if status == "complete" else [],
        "experimental_setup": {
            "datasets_samples": [],
            "protocols": [],
            "baselines_controls": [],
            "metrics": [],
            "hardware_software": [],
        },
        "key_claims": [],
        "limitations": [],
        "artifacts": {
            name: {"reported": None, "items": []}
            for name in ("code", "data", "model", "project", "supplement")
        },
        "credibility": {
            "score": 4 if status == "complete" else None,
            "assessment_confidence": "medium" if status == "complete" else None,
            "rationale": (
                "正文提供了可定位的方法与实验描述。"
                if status == "complete"
                else None
            ),
            "strengths": [],
            "concerns": [],
        },
        "evidence_locators": [],
        "notes": [],
    }


PROTOTYPE_LEDGER = {
    "schema_version": "deepfetch.papers.v4",
    "topic": {
        "input": "核查证据边界",
        "interpretation": "寻找可核查的代表性研究。",
        "search_concepts": ["evidence boundary"],
        "scope_notes": [],
    },
    "run": {
        "intensity": "medium",
        "active_search_budget_minutes": 13,
        "active_search_elapsed_seconds": 42,
        "dimensions_used": [
            "text_queries",
            "literature_roles",
            "citation_graph",
        ],
        "stopping_reason": "coverage_saturated",
    },
    "paper_order": ["doi:10.1000/example"],
    "papers": {
        "doi:10.1000/example": {
            "identity": {
                "paper_id": "doi:10.1000/example",
                "title": "A verifiable paper",
                "doi": "10.1000/example",
                "arxiv_id": None,
                "openalex_id": "W123",
            },
            "metadata": {
                "authors": ["A. Researcher"],
                "institutions": [],
                "year": 2026,
                "venue": "Example Journal",
                "publisher": "Example",
                "abstract": "A verified abstract.",
                "cited_by_count": 1,
                "citation_count_observed_at": "2026-08-22T00:00:00Z",
                "source_urls": ["https://example.org/paper"],
            },
            "pre_understanding": {
                "summary": "摘要支持该论文与问题直接相关。",
                "evidence_level": "abstract_supported",
                "basis": [
                    {
                        "type": "abstract",
                        "source": "https://example.org/paper",
                        "locator": None,
                    }
                ],
                "why_included": "提供核心方法证据。",
                "uncertainty": "外部复现仍待核查。",
            },
            "fulltext_path": "fulltext/example.html",
            "reading": _empty_reading(status="complete"),
        }
    },
    "missing_fulltexts": [],
    "limitations": [],
}

PROTOTYPE_FINAL = {
    "action": "finalize",
    "acquisition_request": None,
    "completion": "complete",
    "limitations": [],
    "workflow": {
        "prototype_commit": PROTOTYPE_COMMIT,
        "main_agent_status": "complete",
        "reader_assignments": [
            {
                "paper_id": "doi:10.1000/example",
                "assignment_id": "reader-assignment-1",
                "reader_agent_ref": "reader-agent-1",
                "status": "complete",
            }
        ],
        "finalize_status": "passed",
        "finalized_at": "2026-08-22T00:00:00Z",
    },
}

PROTOTYPE_ACQUIRE = {
    "action": "acquire",
    "acquisition_request": {
        "request_id": "acq-v4-1",
        "route_policy": "oa_first_then_institution",
        "papers": [
            {
                "paper_id": "doi:10.1000/example",
                "title": "A verifiable paper",
                "doi": "10.1000/example",
                "arxiv_id": None,
                "source_urls": ["https://example.org/paper"],
            }
        ],
    },
    "completion": None,
    "limitations": [],
    "workflow": {
        "prototype_commit": PROTOTYPE_COMMIT,
        "main_agent_status": "running",
        "reader_assignments": [],
        "finalize_status": "pending",
        "finalized_at": None,
    },
}

PROTOTYPE_EMPTY_LEDGER = {
    **copy.deepcopy(PROTOTYPE_LEDGER),
    "paper_order": [],
    "papers": {},
    "missing_fulltexts": [],
    "limitations": ["检索未形成可纳入的精确论文。"],
}

PROTOTYPE_EMPTY_FINAL = {
    "action": "finalize",
    "acquisition_request": None,
    "completion": "honest_empty",
    "limitations": ["检索未形成可纳入的精确论文。"],
    "workflow": {
        "prototype_commit": PROTOTYPE_COMMIT,
        "main_agent_status": "complete",
        "reader_assignments": [],
        "finalize_status": "passed",
        "finalized_at": "2026-08-22T00:00:00Z",
    },
}


class RecordingRunner:
    def __init__(
        self,
        output: dict[str, object],
        *,
        emit_web_evidence: bool = True,
        emit_reader_evidence: bool = True,
        fetch_query: str = "https://example.org/paper",
    ) -> None:
        self.output = output
        self.emit_web_evidence = emit_web_evidence
        self.emit_reader_evidence = emit_reader_evidence
        self.fetch_query = fetch_query
        self.calls: list[tuple[list[str], str, float]] = []
        self.schemas: list[dict[str, object]] = []

    def __call__(
        self, argv: list[str], prompt: str, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append((argv, prompt, timeout))
        _write_empty_v4_artifacts_if_needed(prompt)
        schema_path = Path(argv[argv.index("--output-schema") + 1])
        self.schemas.append(json.loads(schema_path.read_text(encoding="utf-8")))
        result_path = Path(argv[argv.index("--output-last-message") + 1])
        result_path.write_text(json.dumps(self.output, ensure_ascii=False))
        events = [
            {
                "type": "thread.started",
                "thread_id": "native-web-research-1",
            }
        ]
        if self.emit_web_evidence:
            events.extend(
                [
                    {
                        "type": "item.completed",
                        "item": {
                            "id": "search-1",
                            "type": "web_search",
                            "query": "verifiable paper",
                            "action": {"type": "search"},
                        },
                    },
                    {
                        "type": "item.completed",
                        "item": {
                            "id": "fetch-1",
                            "type": "web_search",
                            "query": self.fetch_query,
                            "action": {"type": "other"},
                        },
                    },
                ]
            )
        workflow = self.output.get("workflow")
        if self.emit_reader_evidence and isinstance(workflow, dict):
            assignments = workflow.get("reader_assignments")
            if isinstance(assignments, list):
                for assignment in assignments:
                    if not isinstance(assignment, dict):
                        continue
                    reader_ref = assignment.get("reader_agent_ref")
                    if not isinstance(reader_ref, str):
                        continue
                    events.extend(
                        [
                            _reader_event(
                                "spawn_agent",
                                "native-web-research-1",
                                reader_ref,
                                "pending_init",
                            ),
                            _reader_event(
                                "wait",
                                "native-web-research-1",
                                reader_ref,
                                "completed",
                            ),
                        ]
                    )
        stdout = "\n".join(json.dumps(event) for event in events)
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")


def _write_empty_v4_artifacts_if_needed(prompt: str) -> None:
    marker = "public_output_root="
    output_lines = [
        line.removeprefix(marker)
        for line in prompt.splitlines()
        if line.startswith(marker)
    ]
    if not output_lines:
        return
    output_root = Path(output_lines[0])
    if (output_root / "papers.json").exists():
        return
    (output_root / "fulltext").mkdir(parents=True, exist_ok=True)
    (output_root / "papers.json").write_text(
        json.dumps(PROTOTYPE_EMPTY_LEDGER, ensure_ascii=False),
        encoding="utf-8",
    )
    (output_root / "summary.md").write_text(
        "# 范围\n\n本轮检索未形成可纳入的精确论文。\n",
        encoding="utf-8",
    )


class PrototypeRecordingRunner(RecordingRunner):
    def __call__(
        self, argv: list[str], prompt: str, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        marker = "public_output_root="
        output_line = next(
            line for line in prompt.splitlines() if line.startswith(marker)
        )
        output_root = Path(output_line.removeprefix(marker))
        (output_root / "fulltext").mkdir(parents=True, exist_ok=True)
        fulltext = b"<!doctype html><html><body><article>Verified full text.</article></body></html>"
        digest = hashlib.sha256(fulltext).hexdigest()
        ledger = copy.deepcopy(PROTOTYPE_LEDGER)
        ledger["papers"]["doi:10.1000/example"]["fulltext_path"] = (
            f"fulltext/example-{digest}.html"
        )
        (output_root / "papers.json").write_text(
            json.dumps(ledger, ensure_ascii=False), encoding="utf-8"
        )
        (output_root / "summary.md").write_text(
            "# 范围\n\n跨论文综合结论。[doi:10.1000/example]\n",
            encoding="utf-8",
        )
        (output_root / "fulltext" / f"example-{digest}.html").write_bytes(fulltext)
        return super().__call__(argv, prompt, timeout)


class OversizedPrototypeRunner(RecordingRunner):
    def __call__(
        self, argv: list[str], prompt: str, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        marker = "public_output_root="
        output_root = Path(
            next(line for line in prompt.splitlines() if line.startswith(marker)).removeprefix(marker)
        )
        (output_root / "fulltext").mkdir(parents=True, exist_ok=True)
        ledger = copy.deepcopy(PROTOTYPE_LEDGER)
        ledger["papers"]["doi:10.1000/example"]["fulltext_path"] = "fulltext/oversized.pdf"
        (output_root / "papers.json").write_text(
            json.dumps(ledger, ensure_ascii=False), encoding="utf-8"
        )
        (output_root / "summary.md").write_text(
            "# 范围\n\n超限正文。[doi:10.1000/example]\n", encoding="utf-8"
        )
        with (output_root / "fulltext" / "oversized.pdf").open("wb") as destination:
            destination.truncate(32 * 1024 * 1024 + 1)
        return super().__call__(argv, prompt, timeout)


def _reader_event(
    tool: str,
    sender: str,
    reader_ref: str,
    status: str,
) -> dict[str, object]:
    return {
        "type": "item.completed",
        "item": {
            "id": f"{tool}-{reader_ref}",
            "type": "collab_tool_call",
            "tool": tool,
            "sender_thread_id": sender,
            "receiver_thread_ids": [reader_ref],
            "agents_states": {reader_ref: {"status": status}},
            "status": "completed",
        },
    }


class SequencedPrototypeRunner(PrototypeRecordingRunner):
    def __init__(self, outputs: list[dict[str, object]]) -> None:
        super().__init__(outputs[0])
        self.outputs = outputs

    def __call__(
        self, argv: list[str], prompt: str, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        self.output = self.outputs[len(self.calls)]
        return super().__call__(argv, prompt, timeout)


class RecordingAcquisitionClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, AcquisitionBatchRequest]] = []

    def acquire(
        self, session_ref: str, request: AcquisitionBatchRequest
    ) -> AcquisitionBatchExecution:
        self.calls.append((session_ref, request))
        return AcquisitionBatchExecution(
            request_id=request.request_id,
            session_ref=session_ref,
            status="obtained",
            request=request,
            results=tuple(
                AcquisitionItemResult(
                    paper_id=paper.paper_id,
                    status="obtained",
                    path=f"/private/{paper.paper_id}.html",
                    format="html",
                    failure=None,
                )
                for paper in request.papers
            ),
        )


class WaitingThenReadyAcquisitionClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, AcquisitionBatchRequest]] = []

    def acquire(
        self, session_ref: str, request: AcquisitionBatchRequest
    ) -> AcquisitionBatchExecution:
        self.calls.append((session_ref, request))
        waiting = len(self.calls) == 1
        return AcquisitionBatchExecution(
            request_id=request.request_id,
            session_ref=session_ref,
            status="waiting_user" if waiting else "obtained",
            request=request,
            results=tuple(
                AcquisitionItemResult(
                    paper_id=paper.paper_id,
                    status="waiting_user" if waiting else "obtained",
                    path=None if waiting else f"/private/{paper.paper_id}.html",
                    format=None if waiting else "html",
                    failure=(
                        {
                            "code": "institutional_login_required",
                            "detail": "请在既有浏览器上下文中完成登录。",
                        }
                        if waiting
                        else None
                    ),
                )
                for paper in request.papers
            ),
        )


def _request() -> DeepFetchProviderRequest:
    return DeepFetchProviderRequest(
        request_ref="deepfetch_request_1",
        initialization_id="quest_init_1",
        correlation_ref="deepfetch_correlation_1",
        draft_revision=3,
        draft_hash="a" * 64,
        scope={"goal": "核查证据边界"},
        scope_hash="b" * 64,
        acquisition_session_ref="acquisition_session_1",
        acquisition_config_hash="e" * 64,
        acquisition_runtime_binding_hash="f" * 64,
        accepted_material_bindings=(),
        authorization_receipt=AcceptanceReceipt(
            issuer="human_collaboration",
            kind="first_question_deepfetch_request",
            receipt_ref="hc_receipt_1",
            subject_ref="deepfetch_request_1",
            payload_hash="c" * 64,
        ),
        runtime_binding=DeepFetchRuntimeBinding(
            provider_ref="meta_research.deepfetch.CodexDeepFetchAdapter",
            provider_version="v1",
            model_ref="gpt-test",
            harness_ref="codex-cli",
            capability_bindings=("web-search-live", "web-fetch-live"),
        ),
        run_ref="deepfetch_run_1",
        root_session_ref="deepfetch_session_1",
        attempt_ref="deepfetch_attempt_1",
        attempt_generation=1,
        fence_ref="deepfetch_fence_1",
    )


def test_codex_deepfetch_runs_the_bound_v4_roles_and_imports_only_public_artifacts(
    tmp_path: Path,
) -> None:
    runner = PrototypeRecordingRunner(PROTOTYPE_FINAL)
    adapter = CodexDeepFetchAdapter(
        tmp_path / "provider", model_ref="gpt-test", process_runner=runner
    )

    result = adapter.execute(_request())

    assert result.completion == "complete"
    assert result.summary.startswith("# 范围")
    assert len(result.papers) == 1
    assert len(result.fulltexts) == 1
    assert result.papers_ledger is not None
    assert result.papers_ledger["schema_version"] == "deepfetch.papers.v4"
    assert result.papers_ledger["papers"]["doi:10.1000/example"]["reading"][
        "credibility"
    ]["score"] == 4
    binding = adapter.runtime_binding()
    assert binding.provider_version == PROTOTYPE_COMMIT
    assert {
        "deepfetch-v4-main-agent",
        "hosted-acquisition-session",
        "native-child-readers",
        "papers-v4-finalize",
    } <= set(binding.capability_bindings)
    argv, prompt, _timeout = runner.calls[0]
    assert argv[argv.index("--sandbox") + 1] == "workspace-write"
    assert "DeepFetch v4 main agent" in prompt
    assert "Acquisition" in prompt
    assert "independent Readers" in prompt
    assert "scripts/papers.py finalize" in prompt
    assert set((tmp_path / "provider").glob("runs/*/public/*"))
    assert result.web_evidence is not None
    assert result.web_evidence["prototype"]["prototype_commit"] == (
        PROTOTYPE_COMMIT
    )


def test_codex_deepfetch_rejects_oversized_fulltext_before_validator_reads_it(
    tmp_path: Path,
) -> None:
    adapter = CodexDeepFetchAdapter(
        tmp_path / "provider",
        model_ref="gpt-test",
        process_runner=OversizedPrototypeRunner(PROTOTYPE_FINAL),
    )

    with pytest.raises(DeepFetchUnavailable, match="deepfetch_fulltext_too_large"):
        adapter.execute(_request())


def test_codex_deepfetch_routes_each_finite_batch_through_the_hosted_session(
    tmp_path: Path,
) -> None:
    runner = SequencedPrototypeRunner([PROTOTYPE_ACQUIRE, PROTOTYPE_FINAL])
    acquisition = RecordingAcquisitionClient()
    adapter = CodexDeepFetchAdapter(
        tmp_path / "provider",
        model_ref="gpt-test",
        acquisition_client=acquisition,
        process_runner=runner,
    )

    result = adapter.execute(_request())

    assert len(acquisition.calls) == 1
    session_ref, batch = acquisition.calls[0]
    assert session_ref == "acquisition_session_1"
    assert batch.request_id == "acq-v4-1"
    assert batch.route_policy == "oa_first_then_institution"
    assert len(runner.calls) == 2
    assert "resume" in runner.calls[1][0]
    assert "acquisition_result=" in runner.calls[1][1]
    assert result.web_evidence is not None
    assert result.web_evidence["prototype"]["acquisition_request_ids"] == [
        "acq-v4-1"
    ]


def test_codex_deepfetch_replays_the_exact_pending_batch_after_user_login(
    tmp_path: Path,
) -> None:
    runner = SequencedPrototypeRunner([PROTOTYPE_ACQUIRE, PROTOTYPE_FINAL])
    acquisition = WaitingThenReadyAcquisitionClient()
    adapter = CodexDeepFetchAdapter(
        tmp_path / "provider",
        acquisition_client=acquisition,
        process_runner=runner,
    )

    with pytest.raises(DeepFetchUnavailable) as interrupted:
        adapter.execute(_request())

    assert interrupted.value.code == "deepfetch_acquisition_waiting_user"
    assert interrupted.value.durable_outcome == "pending"
    assert len(runner.calls) == 1
    checkpoint_path = next((tmp_path / "provider" / "runs").glob("*/private/protocol.json"))
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    assert checkpoint["phase"] == "pending_acquisition"
    assert checkpoint["pending_acquisition"]["request_id"] == "acq-v4-1"

    result = adapter.execute(_request())

    assert result.completion == "complete"
    assert len(runner.calls) == 2
    assert [call[1].request_id for call in acquisition.calls] == [
        "acq-v4-1",
        "acq-v4-1",
    ]
    assert acquisition.calls[0][1].identity_payload() == (
        acquisition.calls[1][1].identity_payload()
    )
    assert json.loads(checkpoint_path.read_text(encoding="utf-8"))["phase"] == (
        "finalized"
    )

    replayed = adapter.execute(_request())
    assert replayed.completion == "complete"
    assert len(runner.calls) == 2
    assert len(acquisition.calls) == 2


def test_codex_deepfetch_rejects_unproven_reader_assignments(
    tmp_path: Path,
) -> None:
    runner = PrototypeRecordingRunner(
        PROTOTYPE_FINAL,
        emit_reader_evidence=False,
    )
    adapter = CodexDeepFetchAdapter(
        tmp_path / "provider",
        process_runner=runner,
    )

    with pytest.raises(DeepFetchUnavailable) as rejected:
        adapter.execute(_request())

    assert rejected.value.code == "deepfetch_reader_agent_trace_invalid"


def test_codex_deepfetch_uses_live_web_in_a_workspace_write_root_session(
    tmp_path: Path,
) -> None:
    runner = RecordingRunner(PROTOTYPE_EMPTY_FINAL)
    adapter = CodexDeepFetchAdapter(
        tmp_path / "provider", model_ref="gpt-test", process_runner=runner
    )

    result = adapter.execute(_request())

    assert result.completion == "honest_empty"
    assert result.native_session_ref == "native-web-research-1"
    assert result.web_evidence is not None
    assert result.web_evidence["search_event_count"] == 1
    assert result.web_evidence["fetch_event_count"] == 1
    argv, prompt, timeout = runner.calls[0]
    assert argv[:2] == ["codex", "exec"]
    assert "--ignore-user-config" in argv
    assert "--strict-config" in argv
    assert argv[argv.index("--sandbox") + 1] == "workspace-write"
    config_values = [
        argv[index + 1] for index, value in enumerate(argv) if value == "--config"
    ]
    assert "mcp_servers={}" in config_values
    assert 'approval_policy="never"' in config_values
    assert 'web_search="live"' in config_values
    assert "features.multi_agent=true" in config_values
    assert argv[-1] == "-"
    assert "draft_revision=3" in prompt
    assert "不得把 Cookie、凭据、浏览器 profile" in prompt
    assert timeout > 0
    schema = runner.schemas[0]
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(PROTOTYPE_EMPTY_FINAL)
    assert adapter.runtime_binding().capability_bindings[-2:] == (
        "web-fetch-live",
        "web-search-live",
    )


def test_codex_deepfetch_rejects_a_forged_receipt_field_even_if_runner_bypasses_schema(
    tmp_path: Path,
) -> None:
    runner = RecordingRunner(
        {**PROTOTYPE_EMPTY_FINAL, "receipt": {"status": "accepted"}}
    )
    adapter = CodexDeepFetchAdapter(tmp_path / "provider", process_runner=runner)

    with pytest.raises(DeepFetchUnavailable, match="codex_deepfetch_output_invalid"):
        adapter.execute(_request())


def test_codex_deepfetch_fails_typed_when_live_provider_is_unavailable(
    tmp_path: Path,
) -> None:
    def unavailable(
        argv: list[str], prompt: str, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("codex")

    adapter = CodexDeepFetchAdapter(tmp_path / "provider", process_runner=unavailable)
    with pytest.raises(DeepFetchUnavailable, match="codex_cli_unavailable"):
        adapter.execute(_request())


def test_codex_deepfetch_rejects_model_output_without_real_web_tool_events(
    tmp_path: Path,
) -> None:
    runner = RecordingRunner(PROTOTYPE_EMPTY_FINAL, emit_web_evidence=False)
    adapter = CodexDeepFetchAdapter(tmp_path / "provider", process_runner=runner)

    with pytest.raises(DeepFetchUnavailable, match="deepfetch_web_evidence_invalid"):
        adapter.execute(_request())


def test_codex_deepfetch_accepts_the_real_open_ref_web_event_shape(
    tmp_path: Path,
) -> None:
    runner = RecordingRunner(PROTOTYPE_EMPTY_FINAL, fetch_query="")
    adapter = CodexDeepFetchAdapter(tmp_path / "provider", process_runner=runner)

    result = adapter.execute(_request())

    assert result.web_evidence is not None
    assert result.web_evidence["search_event_count"] == 1
    assert result.web_evidence["fetch_event_count"] == 1


def test_codex_deepfetch_rejects_a_generic_other_event_as_fetch_evidence(
    tmp_path: Path,
) -> None:
    runner = RecordingRunner(PROTOTYPE_EMPTY_FINAL, fetch_query="not-a-fetch")
    adapter = CodexDeepFetchAdapter(tmp_path / "provider", process_runner=runner)

    with pytest.raises(DeepFetchUnavailable, match="deepfetch_web_evidence_invalid"):
        adapter.execute(_request())


@pytest.mark.parametrize(
    ("output", "code"),
    [
        (
            {
                **RESULT,
                "completion": "complete",
                "limitations": [],
            },
            "deepfetch_complete_result_incomplete",
        ),
        (
            {
                **RESULT,
                "completion": "limited",
                "papers": [],
            },
            "deepfetch_limited_result_empty",
        ),
        (
            {
                **RESULT,
                "papers": [
                    {
                        **RESULT["papers"][0],
                        "fulltext_status": "accepted",
                    }
                ],
            },
            "deepfetch_fulltext_status_mismatch",
        ),
        (
            {
                **RESULT,
                "papers": [
                    {
                        **RESULT["papers"][0],
                        "retrieved_at": "yesterday",
                    }
                ],
            },
            "deepfetch_retrieved_at_invalid",
        ),
        (
            {
                **RESULT,
                "papers": [
                    {
                        **RESULT["papers"][0],
                        "url": (
                            "https://example.org/paper?"
                            "X-Amz-Credential=secret&X-Amz-Signature=secret"
                        ),
                    }
                ],
            },
            "deepfetch_paper_url_invalid",
        ),
    ],
)
def test_deepfetch_rejects_semantically_impossible_provider_results(
    output: dict[str, object],
    code: str,
) -> None:
    result = DeepFetchResult(
        completion=output["completion"],  # type: ignore[arg-type]
        summary=str(output["summary"]),
        papers=tuple(output["papers"]),  # type: ignore[arg-type]
        fulltexts=tuple(output["fulltexts"]),  # type: ignore[arg-type]
        limitations=tuple(output["limitations"]),  # type: ignore[arg-type]
        native_session_ref="native-semantic-validation",
        adapter_kind="codex_cli",
        web_evidence={
            "schema_ref": "meta-research/deepfetch-web-evidence/v1",
            "search_event_count": 1,
            "fetch_event_count": 1,
            "trace_hash": "d" * 64,
        },
    )

    with pytest.raises(DeepFetchUnavailable, match=code):
        validate_deepfetch_result(_request(), result)


def test_durable_adapter_resumes_the_same_native_session_after_controlled_stop(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "fake-codex"
    executable.write_text(
        """#!/usr/bin/env python3
import json
import pathlib
import sys
import time

arguments = sys.argv[1:]
result_path = pathlib.Path(arguments[arguments.index('--output-last-message') + 1])
prompt = sys.stdin.read()
thread_ref = 'native-web-research-durable'
print(json.dumps({'type': 'thread.started', 'thread_id': thread_ref}), flush=True)
if 'resume' not in arguments:
    time.sleep(30)
else:
    print(json.dumps({'type': 'item.completed', 'item': {
        'id': 'search-durable', 'type': 'web_search', 'query': 'paper',
        'action': {'type': 'search'}}}), flush=True)
    print(json.dumps({'type': 'item.completed', 'item': {
        'id': 'fetch-durable', 'type': 'web_search',
        'query': 'https://example.org/paper', 'action': {'type': 'other'}}}),
        flush=True)
    output_root = pathlib.Path(next(
        line.split('=', 1)[1] for line in prompt.splitlines()
        if line.startswith('public_output_root=')
    ))
    (output_root / 'fulltext').mkdir(parents=True, exist_ok=True)
    (output_root / 'papers.json').write_text(
        json.dumps({ledger!r}, ensure_ascii=False), encoding='utf-8')
    (output_root / 'summary.md').write_text(
        '# 范围\\n\\n本轮检索未形成可纳入的精确论文。\\n', encoding='utf-8')
    result_path.write_text(json.dumps({result!r}), encoding='utf-8')
""".replace("{result!r}", repr(PROTOTYPE_EMPTY_FINAL)).replace(
            "{ledger!r}", repr(PROTOTYPE_EMPTY_LEDGER)
        ),
        encoding="utf-8",
    )
    executable.chmod(0o700)
    workspace = tmp_path / "provider"
    request = replace(_request(), job_ref="deepfetch-run:durable")
    first = CodexDeepFetchAdapter(
        workspace,
        executable=str(executable),
        model_ref="gpt-test",
        timeout_seconds=60,
    )
    errors: list[BaseException] = []

    def execute_until_stopped() -> None:
        try:
            first.execute(request)
        except BaseException as error:  # pragma: no branch - asserted below
            errors.append(error)

    worker = threading.Thread(target=execute_until_stopped, daemon=True)
    worker.start()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        started_paths = list(
            workspace.glob(
                "provider-operations/*/deepfetch-initial/provider-started.json"
            )
        )
        if started_paths:
            break
        time.sleep(0.02)
    else:  # pragma: no cover - diagnostic for an unusually slow host
        pytest.fail("durable provider did not start")

    first.request_stop()
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], DeepFetchUnavailable)
    assert errors[0].code == "deepfetch_provider_stopped"

    restarted = CodexDeepFetchAdapter(
        workspace,
        executable=str(executable),
        model_ref="gpt-test",
        timeout_seconds=60,
    )
    result = restarted.execute(request)

    assert result.native_session_ref == "native-web-research-durable"
    assert result.web_evidence is not None
    assert (
        list(workspace.glob("provider-operations/*/deepfetch-resume-1/completed.json"))
        == []
    )
    resume_invocations = list(
        workspace.glob("provider-operations/*/deepfetch-resume-1/invocation.json")
    )
    assert len(resume_invocations) == 1
    invocation = json.loads(resume_invocations[0].read_text(encoding="utf-8"))
    assert invocation["payload"]["native_session_ref"] == (
        "native-web-research-durable"
    )


def test_durable_stop_before_thread_start_allows_a_new_provider_operation(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "fake-codex"
    executable.write_text(
        """#!/usr/bin/env python3
import json
import pathlib
import sys
import time

arguments = sys.argv[1:]
result_path = pathlib.Path(arguments[arguments.index('--output-last-message') + 1])
counter_path = pathlib.Path(__file__).with_suffix('.count')
count = int(counter_path.read_text()) + 1 if counter_path.exists() else 1
counter_path.write_text(str(count), encoding='utf-8')
prompt = sys.stdin.read()
if count == 1:
    time.sleep(30)
thread_ref = 'native-after-early-stop'
print(json.dumps({'type': 'thread.started', 'thread_id': thread_ref}), flush=True)
print(json.dumps({'type': 'item.completed', 'item': {
    'id': 'search-after-stop', 'type': 'web_search', 'query': 'paper',
    'action': {'type': 'search'}}}), flush=True)
print(json.dumps({'type': 'item.completed', 'item': {
    'id': 'open-after-stop', 'type': 'web_search', 'query': '',
    'action': {'type': 'other'}}}), flush=True)
output_root = pathlib.Path(next(
    line.split('=', 1)[1] for line in prompt.splitlines()
    if line.startswith('public_output_root=')
))
(output_root / 'fulltext').mkdir(parents=True, exist_ok=True)
(output_root / 'papers.json').write_text(
    json.dumps({ledger!r}, ensure_ascii=False), encoding='utf-8')
(output_root / 'summary.md').write_text(
    '# 范围\\n\\n本轮检索未形成可纳入的精确论文。\\n', encoding='utf-8')
result_path.write_text(json.dumps({result!r}), encoding='utf-8')
""".replace("{result!r}", repr(PROTOTYPE_EMPTY_FINAL)).replace(
            "{ledger!r}", repr(PROTOTYPE_EMPTY_LEDGER)
        ),
        encoding="utf-8",
    )
    executable.chmod(0o700)
    workspace = tmp_path / "provider"
    first_request = replace(_request(), job_ref="deepfetch-run:early-stop:1")
    first = CodexDeepFetchAdapter(
        workspace,
        executable=str(executable),
        model_ref="gpt-test",
        timeout_seconds=60,
    )
    errors: list[BaseException] = []

    def execute_until_stopped() -> None:
        try:
            first.execute(first_request)
        except BaseException as error:  # pragma: no branch - asserted below
            errors.append(error)

    worker = threading.Thread(target=execute_until_stopped, daemon=True)
    worker.start()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if list(
            workspace.glob(
                "provider-operations/*/deepfetch-initial/provider-started.json"
            )
        ):
            break
        time.sleep(0.02)
    else:  # pragma: no cover - diagnostic for an unusually slow host
        pytest.fail("durable provider did not start")

    first.request_stop()
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], DeepFetchUnavailable)
    assert errors[0].code == "deepfetch_provider_stopped"
    assert errors[0].durable_outcome == "pending"

    restarted = CodexDeepFetchAdapter(
        workspace,
        executable=str(executable),
        model_ref="gpt-test",
        timeout_seconds=60,
    )
    with pytest.raises(DeepFetchUnavailable) as reconciled:
        restarted.execute(first_request)
    assert reconciled.value.code == "deepfetch_provider_stopped_before_session"
    assert reconciled.value.durable_outcome == "terminal"
    assert reconciled.value.native_session_ref is None
    assert executable.with_suffix(".count").read_text(encoding="utf-8") == "1"

    result = restarted.execute(
        replace(first_request, job_ref="deepfetch-run:early-stop:2")
    )

    assert result.native_session_ref == "native-after-early-stop"
    assert executable.with_suffix(".count").read_text(encoding="utf-8") == "2"
