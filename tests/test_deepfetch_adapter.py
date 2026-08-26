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
    DEEPFETCH_PROVIDER_STREAM_MAX_BYTES,
    DeepFetchProviderRequest,
    DeepFetchResult,
    DeepFetchRuntimeBinding,
    DeepFetchUnavailable,
    canonical_hash,
    validate_deepfetch_result,
)
from meta_research.owners.common import AcceptanceReceipt
from meta_research.provider_supervisor import (
    ensure_transport_key,
    read_transport_envelope,
    write_exit_receipt,
)
from meta_research.quest_drafting import _CancellableProcessRunner

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
        terminal_reader_tool: str = "wait",
        emit_close_after_wait: bool = False,
        fetch_query: str = "https://example.org/paper",
    ) -> None:
        self.output = output
        self.emit_web_evidence = emit_web_evidence
        self.emit_reader_evidence = emit_reader_evidence
        self.terminal_reader_tool = terminal_reader_tool
        self.emit_close_after_wait = emit_close_after_wait
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
                                self.terminal_reader_tool,
                                "native-web-research-1",
                                reader_ref,
                                "completed",
                            ),
                        ]
                    )
                    if self.emit_close_after_wait:
                        events.append(
                            _reader_event(
                                "close_agent",
                                "native-web-research-1",
                                reader_ref,
                                "completed",
                            )
                        )
        stdout = "\n".join(json.dumps(event) for event in events)
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")


class NamespaceRestrictedRunner(RecordingRunner):
    """Mirror the deployed host where a bubblewrap user namespace cannot start."""

    def __call__(
        self, argv: list[str], prompt: str, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        sandbox = argv[argv.index("--sandbox") + 1]
        if sandbox == "workspace-write":
            self.calls.append((argv, prompt, timeout))
            return subprocess.CompletedProcess(
                argv,
                1,
                stdout=json.dumps(
                    {"type": "thread.started", "thread_id": "native-no-userns"}
                ),
                stderr="bwrap: No permissions to create a new namespace",
            )
        return super().__call__(argv, prompt, timeout)


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


class LifecycleSequencedPrototypeRunner(SequencedPrototypeRunner):
    def __init__(self, outputs: list[dict[str, object]]) -> None:
        super().__init__(outputs)
        self.cancelled_jobs: list[str] = []
        self.finished_jobs: list[str] = []

    def cancel_job(self, job_ref: str) -> None:
        self.cancelled_jobs.append(job_ref)

    def finish_job(self, job_ref: str) -> None:
        self.finished_jobs.append(job_ref)


class DurableSegmentSequenceRunner:
    def __init__(self, workspace: Path, *, stopped_segments: int) -> None:
        self.workspace = workspace
        self.stopped_segments = stopped_segments
        self.calls: list[str] = []

    def __call__(
        self, argv: list[str], prompt: str, timeout: float
    ) -> subprocess.CompletedProcess[str]:  # pragma: no cover - durable seam only
        raise AssertionError("non-durable provider seam used")

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
        del pid_path, supervisor_request_path, timeout
        self.calls.append(job_ref)
        thread_ref = "native-many-durable-segments"
        events = [{"type": "thread.started", "thread_id": thread_ref}]
        result_path = Path(argv[argv.index("--output-last-message") + 1])
        if len(self.calls) > self.stopped_segments:
            events.extend(
                [
                    {
                        "type": "item.completed",
                        "item": {
                            "id": "search-many-segments",
                            "type": "web_search",
                            "query": "verifiable paper",
                            "action": {"type": "search"},
                        },
                    },
                    {
                        "type": "item.completed",
                        "item": {
                            "id": "fetch-many-segments",
                            "type": "web_search",
                            "query": "https://example.org/paper",
                            "action": {"type": "other"},
                        },
                    },
                ]
            )
            _write_empty_v4_artifacts_if_needed(prompt)
            result_path.write_text(
                json.dumps(PROTOTYPE_EMPTY_FINAL), encoding="utf-8"
            )
            termination_reason = "completed"
            returncode = 0
        else:
            termination_reason = "stopped"
            returncode = -15
        stdout_path.write_text(
            "\n".join(json.dumps(event) for event in events), encoding="utf-8"
        )
        _key_path, key = ensure_transport_key(self.workspace)
        invocation = read_transport_envelope(
            stdout_path.parent / "invocation.json", key
        )
        write_exit_receipt(
            stdout_path.parent / "supervisor-exit.json",
            key=key,
            invocation_hash=canonical_hash(invocation),
            prompt_path=stdout_path.parent / "prompt.txt",
            schema_path=stdout_path.parent / "output-schema.json",
            stdout_path=stdout_path,
            result_path=result_path,
            returncode=returncode,
            input_bytes=len(prompt.encode("utf-8")),
            termination_reason=termination_reason,
        )
        return subprocess.CompletedProcess(
            argv, returncode, stdout=stdout_path.read_text(encoding="utf-8"), stderr=""
        )


class OversizedDurableStreamRunner:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace

    def __call__(
        self, argv: list[str], prompt: str, timeout: float
    ) -> subprocess.CompletedProcess[str]:  # pragma: no cover - durable seam only
        raise AssertionError("non-durable provider seam used")

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
        del job_ref, pid_path, supervisor_request_path, timeout
        result_path = Path(argv[argv.index("--output-last-message") + 1])
        result_path.write_text(
            json.dumps(PROTOTYPE_EMPTY_FINAL), encoding="utf-8"
        )
        with stdout_path.open("wb") as stream:
            stream.truncate(DEEPFETCH_PROVIDER_STREAM_MAX_BYTES + 1)
        _key_path, key = ensure_transport_key(self.workspace)
        invocation = read_transport_envelope(
            stdout_path.parent / "invocation.json", key
        )
        write_exit_receipt(
            stdout_path.parent / "supervisor-exit.json",
            key=key,
            invocation_hash=canonical_hash(invocation),
            prompt_path=stdout_path.parent / "prompt.txt",
            schema_path=stdout_path.parent / "output-schema.json",
            stdout_path=stdout_path,
            result_path=result_path,
            returncode=0,
            input_bytes=len(prompt.encode("utf-8")),
        )
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")


class OutputLimitedDurableStreamRunner:
    """Mirror the real supervisor receipt after it caps stdout at the limit."""

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace

    def __call__(
        self, argv: list[str], prompt: str, timeout: float
    ) -> subprocess.CompletedProcess[str]:  # pragma: no cover - durable seam only
        raise AssertionError("non-durable provider seam used")

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
        del job_ref, pid_path, supervisor_request_path, timeout
        result_path = Path(argv[argv.index("--output-last-message") + 1])
        result_path.write_text("", encoding="utf-8")
        stdout_path.write_text(
            json.dumps(
                {"type": "thread.started", "thread_id": "native-output-limit"}
            ),
            encoding="utf-8",
        )
        _key_path, key = ensure_transport_key(self.workspace)
        invocation = read_transport_envelope(
            stdout_path.parent / "invocation.json", key
        )
        write_exit_receipt(
            stdout_path.parent / "supervisor-exit.json",
            key=key,
            invocation_hash=canonical_hash(invocation),
            prompt_path=stdout_path.parent / "prompt.txt",
            schema_path=stdout_path.parent / "output-schema.json",
            stdout_path=stdout_path,
            result_path=result_path,
            returncode=0,
            input_bytes=len(prompt.encode("utf-8")),
            termination_reason="output_limit",
        )
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")


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


def test_non_durable_runner_overflow_maps_to_deepfetch_typed_failure(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "fake-codex-overflow"
    executable.write_text(
        """#!/usr/bin/env python3
import sys

sys.stdout.write('x' * 8192)
sys.stdout.flush()
""",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    adapter = CodexDeepFetchAdapter(
        tmp_path / "provider",
        executable=str(executable),
        model_ref="gpt-test",
        timeout_seconds=10,
        process_runner=_CancellableProcessRunner(stream_max_bytes=1024),
    )

    with pytest.raises(DeepFetchUnavailable) as failure:
        adapter.execute(_request())

    assert failure.value.code == "codex_deepfetch_output_too_large"


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
        "agent-workspace-policy:dedicated-research-workspace-v1",
        "deepfetch-v4-main-agent",
        "filesystem-danger-full-access",
        "hosted-acquisition-session",
        "native-child-readers",
        "papers-v4-finalize",
        "provider-output-limits:stream=67108864;result=1048576",
        "sandbox-policy:danger-full-access",
    } <= set(binding.capability_bindings)
    argv, prompt, _timeout = runner.calls[0]
    assert argv[argv.index("--sandbox") + 1] == "danger-full-access"
    assert argv[argv.index("--cd") + 1] == str(
        tmp_path / "provider" / "research-workspace"
    )
    assert "DeepFetch v4 main agent" in prompt
    assert "Acquisition" in prompt
    assert "independent Readers" in prompt
    assert "scripts/papers.py finalize" in prompt
    assert set((tmp_path / "provider").glob("runs/*/public/*"))
    assert result.web_evidence is not None
    assert result.web_evidence["prototype"]["prototype_commit"] == (
        PROTOTYPE_COMMIT
    )


@pytest.mark.parametrize(
    "file_proof",
    [
        {
            "path": "fulltext/example.html",
            "sha256": "a" * 64,
            "bytes": 41,
            "absolute_path": "/private/custody/example.html",
        },
        {
            "path": "fulltext/example.html",
            "sha256": "a" * 64,
            "bytes": 41,
            "local_uri": "file:///private/custody/example.html",
        },
        {
            "path": "/private/custody/example.html",
            "sha256": "a" * 64,
            "bytes": 41,
        },
        {
            "path": "fulltext/../private/example.html",
            "sha256": "a" * 64,
            "bytes": 41,
        },
        {
            "path": "artifacts/example.html",
            "sha256": "a" * 64,
            "bytes": 41,
        },
        {
            "path": "fulltext/example.txt",
            "sha256": "a" * 64,
            "bytes": 41,
        },
        {
            "path": "fulltext/example.html",
            "sha256": "A" * 64,
            "bytes": 41,
        },
        {
            "path": "fulltext/example.html",
            "sha256": "a" * 63,
            "bytes": 41,
        },
        {
            "path": "fulltext/example.html",
            "sha256": "g" * 64,
            "bytes": 41,
        },
        {
            "path": 41,
            "sha256": "a" * 64,
            "bytes": 41,
        },
        {
            "path": "fulltext/example.html",
            "sha256": "a" * 64,
            "bytes": True,
        },
        {
            "path": "fulltext/example.html",
            "sha256": "a" * 64,
            "bytes": "41",
        },
        {
            "path": "fulltext/example.html",
            "sha256": "a" * 64,
            "bytes": -1,
        },
        "fulltext/example.html",
    ],
    ids=[
        "absolute-path-field",
        "local-uri-field",
        "absolute-path-value",
        "parent-traversal",
        "non-fulltext-root",
        "unsupported-suffix",
        "uppercase-sha256",
        "short-sha256",
        "non-hex-sha256",
        "non-string-path",
        "boolean-byte-count",
        "non-integer-byte-count",
        "negative-byte-count",
        "non-object-proof",
    ],
)
def test_deepfetch_result_rejects_malformed_fulltext_file_proof(
    tmp_path: Path,
    file_proof: object,
) -> None:
    adapter = CodexDeepFetchAdapter(
        tmp_path / "provider",
        model_ref="gpt-test",
        process_runner=PrototypeRecordingRunner(PROTOTYPE_FINAL),
    )
    result = adapter.execute(_request())
    web_evidence = copy.deepcopy(result.web_evidence)
    assert isinstance(web_evidence, dict)
    prototype = web_evidence.get("prototype")
    assert isinstance(prototype, dict)
    prototype["fulltext_files"] = [file_proof]

    with pytest.raises(DeepFetchUnavailable) as failure:
        validate_deepfetch_result(
            _request(), replace(result, web_evidence=web_evidence)
        )

    assert failure.value.code == "deepfetch_prototype_evidence_invalid"


@pytest.mark.parametrize(
    "job_ref",
    [None, "deepfetch-run:large-skill-trace"],
    ids=["non-durable", "durable"],
)
def test_adapter_preserves_large_skill_trace_before_valid_web_evidence(
    tmp_path: Path,
    job_ref: str | None,
) -> None:
    executable = tmp_path / "fake-codex"
    executable.write_text(
        """#!/usr/bin/env python3
import json
import pathlib
import sys

arguments = sys.argv[1:]
result_path = pathlib.Path(arguments[arguments.index('--output-last-message') + 1])
prompt = sys.stdin.read()
print(json.dumps({
    'type': 'thread.started',
    'thread_id': 'native-large-skill-trace',
}), flush=True)
print(json.dumps({
    'type': 'item.completed',
    'item': {
        'id': 'read-deepfetch-v4-references',
        'type': 'command_execution',
        'command': 'read official deepfetch_v4 skill references',
        'aggregated_output': 'x' * (272 * 1024),
        'exit_code': 0,
        'status': 'completed',
    },
}), flush=True)
print(json.dumps({'type': 'item.completed', 'item': {
    'id': 'search-after-skill-read', 'type': 'web_search',
    'query': 'verifiable paper', 'action': {'type': 'search'}}}), flush=True)
print(json.dumps({'type': 'item.completed', 'item': {
    'id': 'fetch-after-skill-read', 'type': 'web_search',
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
    adapter = CodexDeepFetchAdapter(
        workspace,
        executable=str(executable),
        model_ref="gpt-test",
        timeout_seconds=30,
    )

    result = adapter.execute(
        replace(
            _request(),
            runtime_binding=adapter.runtime_binding(),
            job_ref=job_ref,
        )
    )

    assert result.completion == "honest_empty"
    assert result.native_session_ref == "native-large-skill-trace"
    assert result.web_evidence is not None
    assert result.web_evidence["search_event_count"] == 1
    assert result.web_evidence["fetch_event_count"] == 1
    if job_ref is not None:
        stdout_path = next(workspace.glob("provider-operations/*/*/stdout.jsonl"))
        assert stdout_path.stat().st_size > 256 * 1024


def test_durable_adapter_rejects_a_signed_stream_over_the_deepfetch_limit(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "provider"
    adapter = CodexDeepFetchAdapter(
        workspace,
        process_runner=OversizedDurableStreamRunner(workspace),
    )

    with pytest.raises(
        DeepFetchUnavailable, match="codex_deepfetch_output_too_large"
    ):
        adapter.execute(
            replace(
                _request(),
                runtime_binding=adapter.runtime_binding(),
                job_ref="deepfetch-run:oversized-stream",
            )
        )


def test_durable_adapter_types_a_signed_supervisor_output_limit(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "provider"
    adapter = CodexDeepFetchAdapter(
        workspace,
        process_runner=OutputLimitedDurableStreamRunner(workspace),
    )

    with pytest.raises(
        DeepFetchUnavailable, match="codex_deepfetch_output_too_large"
    ):
        adapter.execute(
            replace(
                _request(),
                runtime_binding=adapter.runtime_binding(),
                job_ref="deepfetch-run:supervisor-output-limit",
            )
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


def test_codex_deepfetch_has_no_hidden_provider_turn_generation_limit_across_restart(
    tmp_path: Path,
) -> None:
    acquisition_turns: list[dict[str, object]] = []
    for generation in range(1, 13):
        turn = copy.deepcopy(PROTOTYPE_ACQUIRE)
        request = turn["acquisition_request"]
        assert isinstance(request, dict)
        request["request_id"] = f"acq-v4-{generation}"
        acquisition_turns.append(turn)
    runner = SequencedPrototypeRunner([*acquisition_turns, PROTOTYPE_FINAL])
    acquisition = RecordingAcquisitionClient()
    workspace = tmp_path / "provider"
    adapter = CodexDeepFetchAdapter(
        workspace,
        model_ref="gpt-test",
        acquisition_client=acquisition,
        process_runner=runner,
    )

    result = adapter.execute(_request())

    assert result.completion == "complete"
    assert len(runner.calls) == 13
    assert [call[1].request_id for call in acquisition.calls] == [
        f"acq-v4-{generation}" for generation in range(1, 13)
    ]

    restarted = CodexDeepFetchAdapter(
        workspace,
        model_ref="gpt-test",
        acquisition_client=acquisition,
        process_runner=runner,
    )
    replayed = restarted.execute(_request())

    assert replayed == result
    assert len(runner.calls) == 13
    assert len(acquisition.calls) == 12


def test_codex_deepfetch_cleanup_uses_the_durable_child_operation_registry(
    tmp_path: Path,
) -> None:
    acquisition_turns: list[dict[str, object]] = []
    for generation in range(1, 13):
        turn = copy.deepcopy(PROTOTYPE_ACQUIRE)
        request = turn["acquisition_request"]
        assert isinstance(request, dict)
        request["request_id"] = f"acq-v4-{generation}"
        acquisition_turns.append(turn)
    runner = LifecycleSequencedPrototypeRunner(
        [*acquisition_turns, PROTOTYPE_FINAL]
    )
    adapter = CodexDeepFetchAdapter(
        tmp_path / "provider",
        acquisition_client=RecordingAcquisitionClient(),
        process_runner=runner,
    )
    root_job_ref = "deepfetch-logical-operation"

    adapter.execute(replace(_request(), job_ref=root_job_ref))
    adapter.cancel_job(root_job_ref)
    adapter.finish_job(root_job_ref)

    expected_jobs = {
        root_job_ref,
        *(f"{root_job_ref}:v4-turn:{turn}" for turn in range(13)),
    }
    assert set(runner.cancelled_jobs) == expected_jobs
    assert set(runner.finished_jobs) == expected_jobs
    assert adapter.reconcile_cancelled_job(root_job_ref) is True


def test_codex_deepfetch_has_no_hidden_durable_segment_generation_limit(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "provider"
    runner = DurableSegmentSequenceRunner(workspace, stopped_segments=8)
    adapter = CodexDeepFetchAdapter(workspace, process_runner=runner)

    result = adapter.execute(
        replace(_request(), job_ref="deepfetch-many-segments")
    )

    assert result.completion == "honest_empty"
    assert result.native_session_ref == "native-many-durable-segments"
    assert len(runner.calls) == 9
    assert len(
        list(
            workspace.glob(
                "provider-operations/*/deepfetch-resume-8/supervisor-exit.json"
            )
        )
    ) == 1


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


def test_codex_deepfetch_accepts_completed_close_agent_reader_evidence(
    tmp_path: Path,
) -> None:
    runner = PrototypeRecordingRunner(
        PROTOTYPE_FINAL,
        terminal_reader_tool="close_agent",
    )
    adapter = CodexDeepFetchAdapter(
        tmp_path / "provider",
        process_runner=runner,
    )

    result = adapter.execute(_request())

    assert result.completion == "complete"
    assert result.papers_ledger is not None


def test_codex_deepfetch_deduplicates_wait_and_close_for_one_reader(
    tmp_path: Path,
) -> None:
    runner = PrototypeRecordingRunner(
        PROTOTYPE_FINAL,
        emit_close_after_wait=True,
    )
    adapter = CodexDeepFetchAdapter(
        tmp_path / "provider",
        process_runner=runner,
    )

    result = adapter.execute(_request())

    assert result.completion == "complete"


def test_codex_deepfetch_uses_live_web_in_a_dedicated_full_access_root_session(
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
    assert argv[argv.index("--sandbox") + 1] == "danger-full-access"
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


def test_codex_deepfetch_does_not_require_user_namespaces_on_the_deployed_host(
    tmp_path: Path,
) -> None:
    runner = NamespaceRestrictedRunner(PROTOTYPE_EMPTY_FINAL)
    adapter = CodexDeepFetchAdapter(
        tmp_path / "provider", model_ref="gpt-test", process_runner=runner
    )

    result = adapter.execute(_request())

    assert result.completion == "honest_empty"
    argv, _prompt, _timeout = runner.calls[0]
    assert argv[argv.index("--sandbox") + 1] == "danger-full-access"
    assert {
        "filesystem-danger-full-access",
        "sandbox-policy:danger-full-access",
        "workspace-write-public-artifacts",
    } <= set(adapter.runtime_binding().capability_bindings)


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
pathlib.Path(__file__).with_suffix('.session-started').write_text(
    thread_ref, encoding='utf-8')
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
    session_started = executable.with_suffix(".session-started")
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if session_started.is_file():
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
    counter_path = executable.with_suffix(".count")
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        # The durable provider-started marker is written before the child gets
        # CPU time.  Synchronize on the fixture process itself so cancellation
        # cannot race the assertion that its first invocation was observed.
        if counter_path.exists():
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
    assert counter_path.read_text(encoding="utf-8") == "1"

    result = restarted.execute(
        replace(first_request, job_ref="deepfetch-run:early-stop:2")
    )

    assert result.native_session_ref == "native-after-early-stop"
    assert counter_path.read_text(encoding="utf-8") == "2"
