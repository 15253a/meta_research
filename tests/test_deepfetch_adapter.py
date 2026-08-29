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
from meta_research.codex_ledger import CodexHomeLedgerReader
from meta_research.deepfetch import (
    CodexDeepFetchAdapter,
    DEEPFETCH_PROVIDER_STREAM_MAX_BYTES,
    DeepFetchProviderRequest,
    DeepFetchResult,
    DeepFetchRuntimeBinding,
    DeepFetchUnavailable,
    _read_hosted_acquisition_artifact,
    _verified_codex_reader_ledger_refs,
    canonical_hash,
    validate_deepfetch_result,
)
from meta_research.owners.common import AcceptanceReceipt, OwnerConflict
from meta_research.provider_supervisor import (
    ensure_transport_key,
    read_transport_envelope,
    write_exit_receipt,
)
from meta_research.quest_drafting import (
    PROVIDER_RESULT_MAX_BYTES,
    _CancellableProcessRunner,
)

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
PROTOTYPE_FULLTEXT = (
    b"<!doctype html><html><body><article>Verified full text.</article></body></html>"
)
TAMPERED_FULLTEXT = (
    b"<!doctype html><html><body><article>Tampered full text.</article></body></html>"
)


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
        self, argv: list[str], prompt: str, timeout: float | None
    ) -> subprocess.CompletedProcess[str]:
        if "web_evidence_gate=v1" in prompt:
            self.calls.append((argv, prompt, timeout))
            schema_path = Path(argv[argv.index("--output-schema") + 1])
            self.schemas.append(json.loads(schema_path.read_text(encoding="utf-8")))
            result_path = Path(argv[argv.index("--output-last-message") + 1])
            result_path.write_text(
                json.dumps({"status": "web_evidence_ready"}),
                encoding="utf-8",
            )
            stdout = "\n".join(
                json.dumps(event)
                for event in (
                    {"type": "thread.started", "thread_id": "native-web-research-1"},
                    {
                        "type": "item.completed",
                        "item": {
                            "id": "gate-search-1",
                            "type": "web_search",
                            "query": "verifiable paper",
                            "action": {"type": "search"},
                        },
                    },
                    {
                        "type": "item.completed",
                        "item": {
                            "id": "gate-fetch-1",
                            "type": "web_search",
                            "query": "",
                            "action": {"type": "other"},
                        },
                    },
                )
            )
            return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")
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


class WebEvidenceGateRunner(RecordingRunner):
    """Exercise the short Search+Open gate before the full v4 prompt."""

    def __init__(
        self,
        output: dict[str, object],
        *,
        gate_fetch: bool,
        gate_fetch_query: str = "",
    ) -> None:
        super().__init__(output)
        self.gate_fetch = gate_fetch
        self.gate_fetch_query = gate_fetch_query

    def __call__(
        self, argv: list[str], prompt: str, timeout: float | None
    ) -> subprocess.CompletedProcess[str]:
        if "web_evidence_gate=v1" not in prompt:
            return super().__call__(argv, prompt, timeout)
        self.calls.append((argv, prompt, timeout))
        schema_path = Path(argv[argv.index("--output-schema") + 1])
        self.schemas.append(json.loads(schema_path.read_text(encoding="utf-8")))
        result_path = Path(argv[argv.index("--output-last-message") + 1])
        result_path.write_text(
            json.dumps({"status": "web_evidence_ready"}),
            encoding="utf-8",
        )
        events: list[dict[str, object]] = [
            {"type": "thread.started", "thread_id": "native-web-research-1"},
            {
                "type": "item.completed",
                "item": {
                    "id": "gate-search-1",
                    "type": "web_search",
                    "query": "verifiable paper",
                    "action": {"type": "search"},
                },
            },
        ]
        if self.gate_fetch:
            events.append(
                {
                    "type": "item.completed",
                    "item": {
                        "id": "gate-fetch-1",
                        "type": "web_search",
                        "query": self.gate_fetch_query,
                        "action": {"type": "other"},
                    },
                }
            )
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout="\n".join(json.dumps(event) for event in events),
            stderr="",
        )


class RecordedCodexReaderLedger:
    def __init__(self) -> None:
        self.expected_cwd = ""
        self.root_ref = "native-web-research-1"
        self.child_ref = "native-reader-child-1"
        self.task_name = "/root/reader_agent_1"
        self.terminal = '{"status":"complete"}'

    def read(self, session_ref: str) -> tuple[dict[str, object], ...]:
        context = {
            "type": "turn_context",
            "payload": {
                "cwd": self.expected_cwd,
                "sandbox_policy": {"type": "danger-full-access"},
            },
        }
        if session_ref == self.root_ref:
            call_id = "call-reader-1"
            return (
                {
                    "type": "session_meta",
                    "payload": {
                        "id": self.root_ref,
                        "session_id": self.root_ref,
                        "cwd": self.expected_cwd,
                        "thread_source": "user",
                        "originator": "codex_exec",
                        "source": "exec",
                    },
                },
                context,
                {
                    "type": "response_item",
                    "payload": {
                        "type": "function_call",
                        "name": "spawn_agent",
                        "call_id": call_id,
                        "arguments": json.dumps(
                            {
                                "task_name": "reader_agent_1",
                                "message": "read the hosted artifact",
                                "fork_turns": "all",
                            }
                        ),
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": json.dumps({"task_name": self.task_name}),
                    },
                },
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "sub_agent_activity",
                        "kind": "started",
                        "event_id": call_id,
                        "agent_path": self.task_name,
                        "agent_thread_id": self.child_ref,
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "agent_message",
                        "author": self.task_name,
                        "recipient": "/root",
                        "content": [
                            {
                                "type": "input_text",
                                "text": "Payload:\n" + self.terminal,
                            }
                        ],
                    },
                },
            )
        if session_ref == self.child_ref:
            return (
                {
                    "type": "session_meta",
                    "payload": {
                        "id": self.child_ref,
                        "session_id": self.root_ref,
                        "parent_thread_id": self.root_ref,
                        "cwd": self.expected_cwd,
                        "thread_source": "subagent",
                        "originator": "codex_exec",
                        "source": {
                            "subagent": {
                                "thread_spawn": {
                                    "parent_thread_id": self.root_ref,
                                    "depth": 1,
                                    "agent_path": self.task_name,
                                }
                            }
                        },
                    },
                },
                context,
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "task_complete",
                        "last_agent_message": self.terminal,
                    },
                },
            )
        raise OSError("ledger missing")


def test_codex_home_ledger_reader_rejects_malformed_terminal_jsonl(
    tmp_path: Path,
) -> None:
    codex_home = tmp_path / "codex-home"
    sessions = codex_home / "sessions"
    archived = codex_home / "archived_sessions"
    sessions.mkdir(parents=True)
    archived.mkdir()
    session_ref = "native-ledger-malformed"
    ledger_path = sessions / f"rollout-{session_ref}.jsonl"
    ledger_path.write_text(
        json.dumps(
            {
                "type": "session_meta",
                "payload": {"id": session_ref},
            }
        )
        + "\n{malformed}\n",
        encoding="utf-8",
    )

    with pytest.raises(OSError, match="session ledger invalid"):
        CodexHomeLedgerReader(codex_home.absolute()).read(session_ref)


def test_codex_home_ledger_reader_accepts_complete_json_without_final_newline(
    tmp_path: Path,
) -> None:
    codex_home = tmp_path / "codex-home"
    sessions = codex_home / "sessions"
    archived = codex_home / "archived_sessions"
    sessions.mkdir(parents=True)
    archived.mkdir()
    session_ref = "native-ledger-complete"
    record = {
        "type": "session_meta",
        "payload": {"id": session_ref},
    }
    (sessions / f"rollout-{session_ref}.jsonl").write_text(
        json.dumps(record), encoding="utf-8"
    )

    assert CodexHomeLedgerReader(codex_home.absolute()).read(session_ref) == (
        record,
    )


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
    public_fulltext = PROTOTYPE_FULLTEXT

    def __call__(
        self, argv: list[str], prompt: str, timeout: float | None
    ) -> subprocess.CompletedProcess[str]:
        if "web_evidence_gate=v1" in prompt:
            return super().__call__(argv, prompt, timeout)
        marker = "public_output_root="
        output_line = next(
            line for line in prompt.splitlines() if line.startswith(marker)
        )
        output_root = Path(output_line.removeprefix(marker))
        (output_root / "fulltext").mkdir(parents=True, exist_ok=True)
        fulltext = self.public_fulltext
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


class MissingFulltextPrototypeRunner(RecordingRunner):
    """Write a trustworthy non-empty ledger whose final label is too strong."""

    def __call__(
        self, argv: list[str], prompt: str, timeout: float | None
    ) -> subprocess.CompletedProcess[str]:
        if "web_evidence_gate=v1" not in prompt:
            marker = "public_output_root="
            output_root = Path(
                next(
                    line
                    for line in prompt.splitlines()
                    if line.startswith(marker)
                ).removeprefix(marker)
            )
            (output_root / "fulltext").mkdir(parents=True, exist_ok=True)
            ledger = copy.deepcopy(PROTOTYPE_LEDGER)
            paper_id = "doi:10.1000/example"
            ledger["papers"][paper_id]["fulltext_path"] = None
            ledger["papers"][paper_id]["reading"] = _empty_reading()
            ledger["missing_fulltexts"] = [paper_id]
            ledger["limitations"] = ["该论文没有可合法获取的全文。"]
            (output_root / "papers.json").write_text(
                json.dumps(ledger, ensure_ascii=False), encoding="utf-8"
            )
            (output_root / "summary.md").write_text(
                "# 范围\n\n检索到一篇相关论文，但未取得全文。"
                "[doi:10.1000/example]\n",
                encoding="utf-8",
            )
        return super().__call__(argv, prompt, timeout)


class OversizedPrototypeRunner(RecordingRunner):
    def __call__(
        self, argv: list[str], prompt: str, timeout: float | None
    ) -> subprocess.CompletedProcess[str]:
        if "web_evidence_gate=v1" in prompt:
            return super().__call__(argv, prompt, timeout)
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
    def __init__(
        self,
        outputs: list[dict[str, object]],
        *,
        terminal_reader_tool: str = "wait",
        emit_close_after_wait: bool = False,
    ) -> None:
        super().__init__(
            outputs[0],
            terminal_reader_tool=terminal_reader_tool,
            emit_close_after_wait=emit_close_after_wait,
        )
        self.outputs = outputs

    def __call__(
        self, argv: list[str], prompt: str, timeout: float | None
    ) -> subprocess.CompletedProcess[str]:
        if "web_evidence_gate=v1" not in prompt:
            full_calls = sum(
                "web_evidence_gate=v1" not in recorded_prompt
                for _argv, recorded_prompt, _timeout in self.calls
            )
            self.output = self.outputs[full_calls]
        return super().__call__(argv, prompt, timeout)


class MalformedReaderTraceRunner(SequencedPrototypeRunner):
    def __call__(
        self, argv: list[str], prompt: str, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        completed = super().__call__(argv, prompt, timeout)
        malformed_event = {
            "type": "item.completed",
            "item": {
                "id": "spawn-agent-version-drift",
                "type": "collab_tool_call",
                "tool": "spawn_agent",
                "sender_thread_id": "native-web-research-1",
                "receiver_thread_ids": "reader-agent-version-drift",
                "agents_states": {},
                "status": "completed",
            },
        }
        return subprocess.CompletedProcess(
            argv,
            completed.returncode,
            stdout=f"{completed.stdout}\n{json.dumps(malformed_event)}",
            stderr=completed.stderr,
        )


class NativeReaderIdentityRunner(SequencedPrototypeRunner):
    """Expose the real Codex split between task names and child thread ids."""

    def __call__(
        self, argv: list[str], prompt: str, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        completed = super().__call__(argv, prompt, timeout)
        events = [
            json.loads(line)
            for line in completed.stdout.splitlines()
            if line.strip()
        ]
        for event in events:
            item = event.get("item")
            if not isinstance(item, dict) or item.get("type") != "collab_tool_call":
                continue
            receivers = item.get("receiver_thread_ids")
            states = item.get("agents_states")
            if receivers != ["reader-agent-1"] or not isinstance(states, dict):
                continue
            state = states.pop("reader-agent-1")
            item["receiver_thread_ids"] = ["019-reader-child-thread-uuid"]
            states["019-reader-child-thread-uuid"] = state
        return subprocess.CompletedProcess(
            argv,
            completed.returncode,
            stdout="\n".join(json.dumps(event) for event in events),
            stderr=completed.stderr,
        )


class AdditionalNativeReaderRunner(NativeReaderIdentityRunner):
    def __call__(
        self, argv: list[str], prompt: str, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        completed = super().__call__(argv, prompt, timeout)
        additional_events = [
            _reader_event(
                "spawn_agent",
                "native-web-research-1",
                "019-additional-reader-child",
                "pending_init",
            ),
            _reader_event(
                "wait",
                "native-web-research-1",
                "019-additional-reader-child",
                "completed",
            ),
        ]
        return subprocess.CompletedProcess(
            argv,
            completed.returncode,
            stdout=(
                f"{completed.stdout}\n"
                + "\n".join(json.dumps(event) for event in additional_events)
            ),
            stderr=completed.stderr,
        )


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
            if "web_evidence_gate=v1" in prompt:
                result_path.write_text(
                    json.dumps({"status": "web_evidence_ready"}),
                    encoding="utf-8",
                )
            else:
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
    def __init__(self, artifact_root: Path) -> None:
        self.calls: list[tuple[str, AcquisitionBatchRequest]] = []
        self.artifact_root = artifact_root
        self.executions: dict[tuple[str, str], AcquisitionBatchExecution] = {}

    def acquire(
        self, session_ref: str, request: AcquisitionBatchRequest
    ) -> AcquisitionBatchExecution:
        self.calls.append((session_ref, request))
        self.artifact_root.mkdir(parents=True, exist_ok=True)
        results: list[AcquisitionItemResult] = []
        for paper in request.papers:
            artifact = self.artifact_root / (
                hashlib.sha256(paper.paper_id.encode()).hexdigest() + ".html"
            )
            artifact.write_bytes(PROTOTYPE_FULLTEXT)
            results.append(
                AcquisitionItemResult(
                    paper_id=paper.paper_id,
                    status="obtained",
                    path=str(artifact.resolve()),
                    format="html",
                    failure=None,
                    content_sha256=hashlib.sha256(PROTOTYPE_FULLTEXT).hexdigest(),
                    content_bytes=len(PROTOTYPE_FULLTEXT),
                )
            )
        execution = AcquisitionBatchExecution(
            request_id=request.request_id,
            session_ref=session_ref,
            status="obtained",
            request=_bound_fake_acquisition_request(
                request,
                session_ref,
                self.artifact_root,
            ),
            results=tuple(results),
        )
        self.executions[(session_ref, request.request_id)] = execution
        return execution

    def query_completed_batch(
        self, session_ref: str, request_id: str
    ) -> AcquisitionBatchExecution | None:
        return self.executions.get((session_ref, request_id))


class FileBackedAcquisitionClient:
    def __init__(self, artifact_root: Path, content: bytes) -> None:
        self.artifact_root = artifact_root
        self.content = content
        self.acquire_calls: list[tuple[str, str]] = []
        self.query_calls: list[tuple[str, str]] = []
        self.executions: dict[tuple[str, str], AcquisitionBatchExecution] = {}

    def acquire(
        self, session_ref: str, request: AcquisitionBatchRequest
    ) -> AcquisitionBatchExecution:
        self.acquire_calls.append((session_ref, request.request_id))
        self.artifact_root.mkdir(parents=True, exist_ok=True)
        results: list[AcquisitionItemResult] = []
        for paper in request.papers:
            path = self.artifact_root / f"{hashlib.sha256(paper.paper_id.encode()).hexdigest()}.html"
            path.write_bytes(self.content)
            results.append(
                AcquisitionItemResult(
                    paper_id=paper.paper_id,
                    status="obtained",
                    path=str(path.resolve()),
                    format="html",
                    failure=None,
                    content_sha256=hashlib.sha256(self.content).hexdigest(),
                    content_bytes=len(self.content),
                )
            )
        execution = AcquisitionBatchExecution(
            request_id=request.request_id,
            session_ref=session_ref,
            status="obtained",
            request=_bound_fake_acquisition_request(
                request,
                session_ref,
                self.artifact_root,
            ),
            results=tuple(results),
        )
        self.executions[(session_ref, request.request_id)] = execution
        return execution

    def query_completed_batch(
        self, session_ref: str, request_id: str
    ) -> AcquisitionBatchExecution | None:
        self.query_calls.append((session_ref, request_id))
        return self.executions.get((session_ref, request_id))


class CorruptedOwnerIdentityAcquisitionClient(FileBackedAcquisitionClient):
    def query_completed_batch(
        self, session_ref: str, request_id: str
    ) -> AcquisitionBatchExecution | None:
        self.query_calls.append((session_ref, request_id))
        raise OwnerConflict("acquisition_request_identity_conflict")


class DriftedTerminalAcquisitionClient(FileBackedAcquisitionClient):
    def __init__(self, artifact_root: Path) -> None:
        super().__init__(artifact_root, PROTOTYPE_FULLTEXT)
        self.provider_effect_count = 0

    def acquire(
        self, session_ref: str, request: AcquisitionBatchRequest
    ) -> AcquisitionBatchExecution:
        key = (session_ref, request.request_id)
        existing = self.executions.get(key)
        if existing is not None:
            self.acquire_calls.append(key)
            return existing
        self.provider_effect_count += 1
        execution = super().acquire(session_ref, request)
        for result in execution.results:
            assert result.path is not None
            Path(result.path).write_bytes(TAMPERED_FULLTEXT)
        return execution


class VerifiedTerminalCodexDeepFetchAdapter(CodexDeepFetchAdapter):
    @property
    def requires_verified_terminal_retry(self) -> bool:
        return True


class WaitingThenReadyAcquisitionClient:
    def __init__(self, artifact_root: Path) -> None:
        self.calls: list[tuple[str, AcquisitionBatchRequest]] = []
        self.artifact_root = artifact_root
        self.executions: dict[tuple[str, str], AcquisitionBatchExecution] = {}

    def acquire(
        self, session_ref: str, request: AcquisitionBatchRequest
    ) -> AcquisitionBatchExecution:
        self.calls.append((session_ref, request))
        waiting = len(self.calls) == 1
        self.artifact_root.mkdir(parents=True, exist_ok=True)
        for paper in request.papers:
            artifact = self.artifact_root / (
                hashlib.sha256(paper.paper_id.encode()).hexdigest() + ".html"
            )
            artifact.write_bytes(PROTOTYPE_FULLTEXT)
        execution = AcquisitionBatchExecution(
            request_id=request.request_id,
            session_ref=session_ref,
            status="waiting_user" if waiting else "obtained",
            request=_bound_fake_acquisition_request(
                request,
                session_ref,
                self.artifact_root,
            ),
            results=tuple(
                AcquisitionItemResult(
                    paper_id=paper.paper_id,
                    status="waiting_user" if waiting else "obtained",
                    path=(
                        None
                        if waiting
                        else str(
                            (
                                self.artifact_root
                                / (
                                    hashlib.sha256(paper.paper_id.encode()).hexdigest()
                                    + ".html"
                                )
                            ).resolve()
                        )
                    ),
                    format=None if waiting else "html",
                    failure=(
                        {
                            "code": "institutional_login_required",
                            "detail": "请在既有浏览器上下文中完成登录。",
                        }
                        if waiting
                        else None
                    ),
                    content_sha256=(
                        None
                        if waiting
                        else hashlib.sha256(PROTOTYPE_FULLTEXT).hexdigest()
                    ),
                    content_bytes=None if waiting else len(PROTOTYPE_FULLTEXT),
                )
                for paper in request.papers
            ),
        )
        if execution.status != "waiting_user":
            self.executions[(session_ref, request.request_id)] = execution
        return execution

    def query_completed_batch(
        self, session_ref: str, request_id: str
    ) -> AcquisitionBatchExecution | None:
        return self.executions.get((session_ref, request_id))


def _bound_fake_acquisition_request(
    request: AcquisitionBatchRequest,
    session_ref: str,
    artifact_root: Path,
) -> AcquisitionBatchRequest:
    return request.bind_to_session(
        session_ref=session_ref,
        session_mode="oa_then_institution",
        browser_context_ref=None,
        provider_state_dir=artifact_root.parent,
        target_dir=artifact_root,
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
            capability_bindings=(
                "codex-reader-ledger:v1",
                "web-evidence-gate:v1",
                "web-search-live",
                "web-fetch-live",
            ),
        ),
        run_ref="deepfetch_run_1",
        root_session_ref="deepfetch_session_1",
        attempt_ref="deepfetch_attempt_1",
        attempt_generation=1,
        fence_ref="deepfetch_fence_1",
    )


def _execute(
    adapter: CodexDeepFetchAdapter,
    request: DeepFetchProviderRequest | None = None,
) -> DeepFetchResult:
    bound = request or _request()
    return adapter.execute(
        replace(bound, runtime_binding=adapter.runtime_binding())
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
        process_runner=_CancellableProcessRunner(stream_max_bytes=1024),
    )

    with pytest.raises(DeepFetchUnavailable) as failure:
        _execute(adapter)

    assert failure.value.code == "codex_deepfetch_output_too_large"


def test_codex_deepfetch_runs_the_bound_v4_roles_and_imports_only_public_artifacts(
    tmp_path: Path,
) -> None:
    runner = SequencedPrototypeRunner([PROTOTYPE_ACQUIRE, PROTOTYPE_FINAL])
    acquisition = RecordingAcquisitionClient(tmp_path / "owner-artifacts")
    adapter = CodexDeepFetchAdapter(
        tmp_path / "provider",
        model_ref="gpt-test",
        acquisition_client=acquisition,
        process_runner=runner,
    )

    result = _execute(adapter)

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
        "agent-workspace-policy:provider-operation-scoped-v2",
        "codex-reader-ledger:v1",
        "deepfetch-v4-main-agent",
        "filesystem-danger-full-access",
        "hosted-acquisition-session",
        "native-child-readers",
        "papers-v4-finalize",
        (
            "provider-output-limits:"
            f"stream={DEEPFETCH_PROVIDER_STREAM_MAX_BYTES};"
            f"result={PROVIDER_RESULT_MAX_BYTES}"
        ),
        "sandbox-policy:danger-full-access",
        "web-evidence-gate:v1",
    } <= set(binding.capability_bindings)
    argv, prompt, _timeout = runner.calls[1]
    assert argv[argv.index("--sandbox") + 1] == "danger-full-access"
    agent_workspace = Path(argv[argv.index("--cd") + 1])
    assert agent_workspace.parent == tmp_path / "provider" / "research-workspaces"
    assert agent_workspace.is_dir()
    assert "DeepFetch v4 main agent" in prompt
    assert "Acquisition" in prompt
    assert "independent Readers" in prompt
    assert "scripts/papers.py finalize" in prompt
    assert len(acquisition.calls) == 1
    assert set((tmp_path / "provider").glob("runs/*/public/*"))
    assert result.web_evidence is not None
    assert result.web_evidence["prototype"]["prototype_commit"] == (
        PROTOTYPE_COMMIT
    )


def test_codex_deepfetch_preserves_limited_results_mislabeled_complete(
    tmp_path: Path,
) -> None:
    output = copy.deepcopy(PROTOTYPE_FINAL)
    output["completion"] = "complete"
    output["limitations"] = ["该论文没有可合法获取的全文。"]
    workflow = output["workflow"]
    assert isinstance(workflow, dict)
    workflow["reader_assignments"] = []
    adapter = CodexDeepFetchAdapter(
        tmp_path / "provider",
        process_runner=MissingFulltextPrototypeRunner(output),
    )

    result = _execute(adapter)

    assert result.completion == "limited"
    assert len(result.papers) == 1
    assert result.fulltexts == ()
    assert result.limitations == ("该论文没有可合法获取的全文。",)


def test_codex_deepfetch_still_rejects_empty_result_mislabeled_complete(
    tmp_path: Path,
) -> None:
    output = copy.deepcopy(PROTOTYPE_EMPTY_FINAL)
    output["completion"] = "complete"
    adapter = CodexDeepFetchAdapter(
        tmp_path / "provider",
        process_runner=RecordingRunner(output),
    )

    with pytest.raises(DeepFetchUnavailable) as failure:
        _execute(adapter)

    assert failure.value.code == "deepfetch_complete_result_incomplete"


def test_codex_deepfetch_explains_evidence_completion_in_every_final_prompt(
    tmp_path: Path,
) -> None:
    runner = SequencedPrototypeRunner([PROTOTYPE_ACQUIRE, PROTOTYPE_FINAL])
    adapter = CodexDeepFetchAdapter(
        tmp_path / "provider",
        acquisition_client=RecordingAcquisitionClient(
            tmp_path / "owner-artifacts"
        ),
        process_runner=runner,
    )

    _execute(adapter)

    completion_rule = "completion 描述证据完备性，不是流程是否执行完"
    for _argv, prompt, _timeout in runner.calls[1:]:
        assert completion_rule in prompt
        assert "complete：papers 非空" in prompt
        assert "limited：papers 非空且 limitations 非空" in prompt
        assert "honest_empty：papers 为空" in prompt
    completion_schema = runner.schemas[1]["properties"]["completion"]
    assert completion_rule in completion_schema["description"]
    assert "workflow.main_agent_status=complete" in completion_schema["description"]


def test_codex_deepfetch_rejects_noncurrent_binding_before_any_effect(
    tmp_path: Path,
) -> None:
    runner = SequencedPrototypeRunner([PROTOTYPE_EMPTY_FINAL])
    acquisition = RecordingAcquisitionClient(tmp_path / "owner-artifacts")
    workspace = tmp_path / "provider"
    adapter = CodexDeepFetchAdapter(
        workspace,
        acquisition_client=acquisition,
        process_runner=runner,
    )
    current = adapter.runtime_binding()
    old_binding = replace(current, model_ref="gpt-stale")

    with pytest.raises(DeepFetchUnavailable) as failure:
        adapter.execute(replace(_request(), runtime_binding=old_binding))

    assert failure.value.code == "deepfetch_runtime_binding_transition_required"
    assert failure.value.durable_outcome == "pending"
    assert canonical_hash(old_binding.as_dict()) != canonical_hash(
        current.as_dict()
    )
    assert runner.calls == []
    assert acquisition.calls == []
    assert not (workspace / "runs").exists()
    assert not (workspace / "provider-operations").exists()


def test_codex_deepfetch_retires_signed_gate_ack_loss_without_new_effect(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "provider"
    runner = DurableSegmentSequenceRunner(workspace, stopped_segments=0)
    acquisition = RecordingAcquisitionClient(tmp_path / "owner-artifacts")
    adapter = CodexDeepFetchAdapter(
        workspace,
        acquisition_client=acquisition,
        process_runner=runner,
    )
    request = replace(
        _request(),
        runtime_binding=adapter.runtime_binding(),
        job_ref="deepfetch-run:signed-gate-ack-loss",
    )
    adapter.execute(request)
    checkpoint_path = next(
        (workspace / "runs").glob("*/private/protocol.json")
    )
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    checkpoint.update(
        {
            "phase": "ready_for_turn",
            "native_session_ref": None,
            "next_turn_number": 0,
            "evidence_parts": [],
            "acquisition_request_ids": [],
            "acquisition_item_proofs": [],
            "pending_acquisition": None,
            "next_prompt": adapter._web_evidence_gate_prompt(request),
            "final_envelope": None,
        }
    )
    checkpoint_path.write_text(
        json.dumps(
            checkpoint,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    checkpoint_before = checkpoint_path.read_bytes()
    calls_before = tuple(runner.calls)

    with pytest.raises(DeepFetchUnavailable) as failure:
        adapter.execute(replace(request, reconcile_only=True))

    assert failure.value.code == "deepfetch_runtime_binding_transition_required"
    assert failure.value.durable_outcome == "terminal"
    assert tuple(runner.calls) == calls_before
    assert acquisition.calls == []
    assert checkpoint_path.read_bytes() == checkpoint_before


def test_codex_deepfetch_treats_selected_oa_as_the_primary_route(
    tmp_path: Path,
) -> None:
    runner = RecordingRunner(PROTOTYPE_EMPTY_FINAL)
    adapter = CodexDeepFetchAdapter(
        tmp_path / "provider", process_runner=runner
    )

    _execute(
        adapter,
        replace(
            _request(),
            scope={
                "goal": "核查证据边界",
                "literature_mode": "oa_only",
                "library_entry_url": "",
            },
        )
    )

    prompt = runner.calls[1][1]
    assert "oa_only 是用户明确选择的主路线" in prompt
    assert "跳过 institution/browser preflight" in prompt
    assert "不得描述为被迫、只能或降级到 OA" in prompt
    assert "不得直接运行 Nature Downloader" in prompt
    assert "需要全文时必须先返回 action=acquire" in prompt


def test_deepfetch_activity_tail_projects_live_events_without_provider_content(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "provider"
    adapter = CodexDeepFetchAdapter(workspace)
    binding = adapter.runtime_binding()
    binding_hash = canonical_hash(binding.as_dict())
    job_ref = "deepfetch-run:activity"
    provider_job_ref = f"{job_ref}:v4-turn:0"
    adapter._register_provider_turn(
        root_job_ref=job_ref,
        turn_number=0,
        provider_job_ref=provider_job_ref,
        runtime_binding_hash=binding_hash,
    )
    operation = adapter._provider_operation_root(provider_job_ref, binding_hash)
    segment = operation / "deepfetch-initial"
    segment.mkdir(parents=True)
    sensitive = "SECRET_QUERY https://private.example/paper /private/workspace"
    events = [
        {"type": "thread.started", "thread_id": sensitive},
        {"type": "turn.started"},
        {
            "type": "item.started",
            "item": {
                "id": "search-1",
                "type": "web_search",
                "query": sensitive,
                "action": {"type": "search"},
            },
        },
        {
            "type": "item.completed",
            "item": {
                "id": "fetch-1",
                "type": "web_search",
                "query": sensitive,
                "action": {"type": "other"},
            },
        },
        {
            "type": "item.started",
            "item": {
                "id": "command-1",
                "type": "command_execution",
                "command": sensitive,
                "aggregated_output": sensitive,
                "status": "in_progress",
            },
        },
        {
            "type": "item.completed",
            "item": {
                "id": "file-1",
                "type": "file_change",
                "status": "completed",
                "changes": [{"path": sensitive, "kind": "update"}],
            },
        },
        {
            "type": "item.completed",
            "item": {
                "id": "child-1",
                "type": "collab_tool_call",
                "status": "completed",
                "sender_thread_id": sensitive,
                "receiver_thread_ids": [sensitive],
            },
        },
    ]
    stdout = segment / "stdout.jsonl"
    stdout.write_bytes(
        b"".join(
            json.dumps(event).encode("utf-8") + b"\n" for event in events
        )
        + b'{"type":"item.started","item":'
    )
    raw_before = stdout.read_bytes()

    projected = adapter.recent_activity_events(job_ref, binding_hash)

    assert [event["label"] for event in projected] == [
        "DeepFetch 会话已启动",
        "正在规划研究步骤",
        "正在执行 Web Search",
        "Web 资料读取完成",
        "正在处理研究资料",
        "研究记录已更新",
        "研究子任务已返回",
    ]
    assert [event["sequence"] for event in projected] == sorted(
        event["sequence"] for event in projected
    )
    assert sensitive not in json.dumps(projected, ensure_ascii=False)
    assert all(
        set(event) == {"sequence", "category", "status", "label"}
        for event in projected
    )
    assert stdout.read_bytes() == raw_before

    with stdout.open("ab") as stream:
        stream.write(
            json.dumps(
                {
                    "id": "message-1",
                    "type": "agent_message",
                    "text": sensitive,
                }
            ).encode("utf-8")
            + b"}\n"
        )

    updated = adapter.recent_activity_events(job_ref, binding_hash)
    assert updated[-1]["label"] == "正在更新研究判断"
    assert sensitive not in json.dumps(updated, ensure_ascii=False)
    assert stdout.read_bytes().startswith(raw_before)
    assert sensitive.encode("utf-8") in stdout.read_bytes()


@pytest.mark.parametrize(
    "limitation",
    [
        "institutional browser route 不可用，因此只能按 `oa_only` 路径继续。",
        "被迫使用 OA-only。",
        "We were forced to use OA-only.",
        "检索路线降级到 OA-only。",
    ],
)
def test_codex_deepfetch_rejects_forced_fallback_language_for_selected_oa(
    tmp_path: Path,
    limitation: str,
) -> None:
    misleading = copy.deepcopy(PROTOTYPE_EMPTY_FINAL)
    misleading["limitations"] = [limitation]
    adapter = CodexDeepFetchAdapter(
        tmp_path / "provider",
        model_ref="gpt-test",
        process_runner=RecordingRunner(misleading),
    )

    with pytest.raises(
        DeepFetchUnavailable,
        match="deepfetch_oa_only_limitation_invalid",
    ):
        _execute(
            adapter,
            replace(
                _request(),
                scope={
                    "goal": "核查证据边界",
                    "literature_mode": "oa_only",
                    "library_entry_url": "",
                },
            )
        )


@pytest.mark.parametrize(
    "limitation",
    [
        "OA-only 路线只能获取到 2 篇，其余未找到。",
        "OA-only 路线只能使用公开可访问的来源，不覆盖订阅内容。",
        "OA-only 路线只能通过开放仓储与作者主页核验全文。",
        "The OA-only route can only use openly accessible copies.",
        "OA-only 并非被迫降级，而是用户明确选择。",
        "The OA-only route was not forced; it was explicitly selected.",
        "OA-only 并不是 fallback。",
        "并非被迫使用 OA-only，而是用户主动选择。",
        "不是被迫采用 OA-only。",
        "没有降级到 OA-only，原本就选择 OA-only。",
        "We were not forced to use OA-only.",
    ],
)
def test_codex_deepfetch_accepts_honest_oa_only_scope_limitations(
    tmp_path: Path,
    limitation: str,
) -> None:
    honest = copy.deepcopy(PROTOTYPE_EMPTY_FINAL)
    honest["limitations"] = [limitation]
    adapter = CodexDeepFetchAdapter(
        tmp_path / "provider",
        model_ref="gpt-test",
        process_runner=RecordingRunner(honest),
    )

    result = _execute(
        adapter,
        replace(
            _request(),
            scope={
                "goal": "核查证据边界",
                "literature_mode": "oa_only",
                "library_entry_url": "",
            },
        )
    )

    assert limitation in result.limitations


@pytest.mark.parametrize(
    "summary",
    [
        "机构访问不可用，因此被迫使用 OA-only。",
        "We were forced to use OA-only because institutional access was unavailable.",
        "检索路线降级到 OA-only。",
    ],
)
def test_deepfetch_rejects_forced_fallback_language_in_selected_oa_summary(
    summary: str,
) -> None:
    request = replace(
        _request(),
        scope={"goal": "核查证据边界", "literature_mode": "oa_only"},
        runtime_binding=replace(
            _request().runtime_binding,
            provider_ref="test.deepfetch.provider",
        ),
    )
    result = DeepFetchResult(
        completion="honest_empty",
        summary=summary,
        papers=(),
        fulltexts=(),
        limitations=("OA-only 路线未找到符合范围的论文。",),
        native_session_ref="native-oa-summary-validation",
        adapter_kind="test",
        web_evidence=None,
    )

    with pytest.raises(
        DeepFetchUnavailable,
        match="^deepfetch_oa_only_limitation_invalid$",
    ):
        validate_deepfetch_result(request, result)


@pytest.mark.parametrize(
    "summary",
    [
        "OA-only 是用户明确选择的主路线，本轮仅核查公开可访问的副本。",
        "OA-only 并非被迫降级，而是用户明确选择。",
        "The OA-only route was not forced; it was explicitly selected.",
    ],
)
def test_deepfetch_accepts_honest_selected_oa_summary(summary: str) -> None:
    request = replace(
        _request(),
        scope={"goal": "核查证据边界", "literature_mode": "oa_only"},
        runtime_binding=replace(
            _request().runtime_binding,
            provider_ref="test.deepfetch.provider",
        ),
    )
    result = DeepFetchResult(
        completion="honest_empty",
        summary=summary,
        papers=(),
        fulltexts=(),
        limitations=("OA-only 路线未找到符合范围的论文。",),
        native_session_ref="native-oa-summary-validation",
        adapter_kind="test",
        web_evidence=None,
    )

    payload, _result_hash = validate_deepfetch_result(request, result)

    assert payload["summary"] == summary


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
        acquisition_client=RecordingAcquisitionClient(
            tmp_path / "owner-artifacts"
        ),
        process_runner=SequencedPrototypeRunner(
            [PROTOTYPE_ACQUIRE, PROTOTYPE_FINAL]
        ),
    )
    result = _execute(adapter)
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
if 'web_evidence_gate=v1' in prompt:
    result_path.write_text(
        json.dumps({'status': 'web_evidence_ready'}), encoding='utf-8')
    raise SystemExit(0)
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
    )

    provider_request = replace(
        _request(),
        runtime_binding=adapter.runtime_binding(),
        job_ref=job_ref,
    )
    result = adapter.execute(provider_request)

    assert result.completion == "honest_empty"
    assert result.native_session_ref == "native-large-skill-trace"
    assert result.web_evidence is not None
    assert result.web_evidence["search_event_count"] == 2
    assert result.web_evidence["fetch_event_count"] == 2
    if job_ref is not None:
        _key_path, key = ensure_transport_key(workspace)
        supervisor_requests = [
            read_transport_envelope(path, key)
            for path in workspace.glob(
                "provider-operations/*/*/supervisor-request.json"
            )
        ]
        assert [
            request["timeout_seconds"] for request in supervisor_requests
        ] == [None, None]
        reconciled = adapter.execute(
            replace(
                provider_request,
                native_session_ref=result.native_session_ref,
                reconcile_only=True,
            )
        )
        assert reconciled == result
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
        _execute(adapter)


def test_codex_deepfetch_routes_each_finite_batch_through_the_hosted_session(
    tmp_path: Path,
) -> None:
    runner = SequencedPrototypeRunner([PROTOTYPE_ACQUIRE, PROTOTYPE_FINAL])
    acquisition = RecordingAcquisitionClient(tmp_path / "owner-artifacts")
    adapter = CodexDeepFetchAdapter(
        tmp_path / "provider",
        model_ref="gpt-test",
        acquisition_client=acquisition,
        process_runner=runner,
    )

    result = _execute(adapter)

    assert len(acquisition.calls) == 1
    session_ref, batch = acquisition.calls[0]
    assert session_ref == "acquisition_session_1"
    assert batch.request_id == "acq-v4-1"
    assert batch.route_policy == "oa_first_then_institution"
    assert len(runner.calls) == 3
    assert "resume" in runner.calls[2][0]
    assert "acquisition_result=" in runner.calls[2][1]
    assert "acquisition_artifact_proofs=" in runner.calls[2][1]
    assert "不得按同一 paper_id 自造或替换正文 bytes" in runner.calls[2][1]
    checkpoint_path = next(
        (tmp_path / "provider" / "runs").glob("*/private/protocol.json")
    )
    proof = json.loads(checkpoint_path.read_text(encoding="utf-8"))[
        "acquisition_item_proofs"
    ][0]
    assert proof == {
        "request_id": "acq-v4-1",
        "paper_id": "doi:10.1000/example",
        "status": "obtained",
        "path": str(
            (
                tmp_path
                / "owner-artifacts"
                / (
                    hashlib.sha256(b"doi:10.1000/example").hexdigest()
                    + ".html"
                )
            ).resolve()
        ),
        "format": "html",
        "sha256": hashlib.sha256(PROTOTYPE_FULLTEXT).hexdigest(),
        "bytes": len(PROTOTYPE_FULLTEXT),
    }
    assert result.web_evidence is not None
    assert result.web_evidence["prototype"]["acquisition_request_ids"] == [
        "acq-v4-1"
    ]


@pytest.mark.parametrize("request_id", ["/tmp/x", "../x", "a/b"])
def test_codex_deepfetch_rejects_path_unsafe_acquisition_request_id_before_hosting(
    tmp_path: Path,
    request_id: str,
) -> None:
    output = copy.deepcopy(PROTOTYPE_ACQUIRE)
    acquisition_request = output["acquisition_request"]
    assert isinstance(acquisition_request, dict)
    acquisition_request["request_id"] = request_id
    acquisition = RecordingAcquisitionClient(tmp_path / "owner-artifacts")
    adapter = CodexDeepFetchAdapter(
        tmp_path / "provider",
        model_ref="gpt-test",
        acquisition_client=acquisition,
        process_runner=PrototypeRecordingRunner(output),
    )

    with pytest.raises(
        DeepFetchUnavailable,
        match="deepfetch_acquisition_request_invalid",
    ):
        _execute(adapter)

    assert acquisition.calls == []


def test_hosted_acquisition_reader_rejects_a_symlinked_owner_target_root(
    tmp_path: Path,
) -> None:
    relocated_root = tmp_path / "relocated-owner-target"
    relocated_root.mkdir()
    artifact = relocated_root / "paper.html"
    artifact.write_bytes(PROTOTYPE_FULLTEXT)
    nominal_root = tmp_path / "owner-target"
    nominal_root.symlink_to(relocated_root, target_is_directory=True)

    with pytest.raises(
        DeepFetchUnavailable,
        match="deepfetch_acquisition_artifact_invalid",
    ):
        _read_hosted_acquisition_artifact(
            str(artifact.resolve()),
            "html",
            required_root=nominal_root,
        )


def test_pending_acquisition_proof_drift_requires_a_successor_without_replay(
    tmp_path: Path,
) -> None:
    acquisition = DriftedTerminalAcquisitionClient(
        tmp_path / "owner-artifacts"
    )
    runner = PrototypeRecordingRunner(PROTOTYPE_ACQUIRE)
    adapter = VerifiedTerminalCodexDeepFetchAdapter(
        tmp_path / "provider",
        model_ref="gpt-test",
        acquisition_client=acquisition,
        process_runner=runner,
    )

    with pytest.raises(DeepFetchUnavailable) as first_failure:
        _execute(adapter)

    assert first_failure.value.code == "deepfetch_acquisition_artifact_drift"
    assert first_failure.value.durable_outcome == "pending"
    assert first_failure.value.native_session_ref == "native-web-research-1"
    assert acquisition.provider_effect_count == 1
    assert len(acquisition.acquire_calls) == 1
    assert len(runner.calls) == 2

    with pytest.raises(DeepFetchUnavailable) as repeated_failure:
        _execute(adapter)

    assert repeated_failure.value.code == "deepfetch_acquisition_artifact_drift"
    assert repeated_failure.value.durable_outcome == "pending"
    assert acquisition.provider_effect_count == 1
    assert len(acquisition.acquire_calls) == 2
    assert len(runner.calls) == 2


def test_codex_deepfetch_rejects_reader_fulltext_without_hosted_acquisition_proof(
    tmp_path: Path,
) -> None:
    adapter = CodexDeepFetchAdapter(
        tmp_path / "provider",
        model_ref="gpt-test",
        process_runner=PrototypeRecordingRunner(PROTOTYPE_FINAL),
    )

    with pytest.raises(
        DeepFetchUnavailable,
        match="deepfetch_hosted_acquisition_proof_missing",
    ):
        _execute(adapter)


def test_codex_deepfetch_binds_each_reader_fulltext_to_an_obtained_hosted_item(
    tmp_path: Path,
) -> None:
    unrelated_acquisition = copy.deepcopy(PROTOTYPE_ACQUIRE)
    unrelated_paper = unrelated_acquisition["acquisition_request"]["papers"][0]
    unrelated_paper.update(
        {
            "paper_id": "doi:10.1000/unrelated",
            "title": "An unrelated acquired paper",
            "doi": "10.1000/unrelated",
        }
    )
    adapter = CodexDeepFetchAdapter(
        tmp_path / "provider",
        model_ref="gpt-test",
        acquisition_client=RecordingAcquisitionClient(
            tmp_path / "owner-artifacts"
        ),
        process_runner=SequencedPrototypeRunner(
            [unrelated_acquisition, PROTOTYPE_FINAL]
        ),
    )

    with pytest.raises(
        DeepFetchUnavailable,
        match="deepfetch_hosted_acquisition_proof_mismatch",
    ):
        _execute(adapter)


def test_codex_deepfetch_rejects_reader_bytes_not_returned_by_hosted_acquisition(
    tmp_path: Path,
) -> None:
    adapter = CodexDeepFetchAdapter(
        tmp_path / "provider",
        model_ref="gpt-test",
        acquisition_client=FileBackedAcquisitionClient(
            tmp_path / "owner-artifacts",
            b"<html><body>Different hosted artifact.</body></html>",
        ),
        process_runner=SequencedPrototypeRunner(
            [PROTOTYPE_ACQUIRE, PROTOTYPE_FINAL]
        ),
    )

    with pytest.raises(
        DeepFetchUnavailable,
        match="deepfetch_hosted_acquisition_artifact_mismatch",
    ):
        _execute(adapter)


def test_codex_deepfetch_rejects_owner_artifact_mutated_after_terminal_commit(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "provider"
    artifact_root = tmp_path / "owner-artifacts"
    artifact_path = artifact_root / (
        hashlib.sha256(b"doi:10.1000/example").hexdigest() + ".html"
    )
    acquisition = FileBackedAcquisitionClient(
        artifact_root,
        PROTOTYPE_FULLTEXT,
    )
    runner = SequencedPrototypeRunner([PROTOTYPE_ACQUIRE, PROTOTYPE_FINAL])
    adapter = CodexDeepFetchAdapter(
        workspace,
        model_ref="gpt-test",
        acquisition_client=acquisition,
        process_runner=runner,
    )
    _execute(adapter)
    artifact_path.write_bytes(TAMPERED_FULLTEXT)
    public_root = next(workspace.glob("runs/*/public"))
    old_public = next((public_root / "fulltext").glob("*.html"))
    tampered_digest = hashlib.sha256(TAMPERED_FULLTEXT).hexdigest()
    new_relative = f"fulltext/example-{tampered_digest}.html"
    new_public = public_root / new_relative
    old_public.rename(new_public)
    new_public.write_bytes(TAMPERED_FULLTEXT)
    ledger_path = public_root / "papers.json"
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    ledger["papers"]["doi:10.1000/example"]["fulltext_path"] = new_relative
    ledger_path.write_text(json.dumps(ledger, ensure_ascii=False), encoding="utf-8")
    checkpoint_path = next(workspace.glob("runs/*/private/protocol.json"))
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    checkpoint["acquisition_item_proofs"][0]["sha256"] = tampered_digest
    checkpoint["acquisition_item_proofs"][0]["bytes"] = len(TAMPERED_FULLTEXT)
    checkpoint_path.write_text(
        json.dumps(
            checkpoint,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )

    with pytest.raises(
        DeepFetchUnavailable,
        match="deepfetch_acquisition_artifact_drift",
    ):
        restarted = CodexDeepFetchAdapter(
            workspace,
            model_ref="gpt-test",
            acquisition_client=acquisition,
            process_runner=runner,
        )
        _execute(restarted)

    assert acquisition.acquire_calls == [("acquisition_session_1", "acq-v4-1")]


def test_codex_deepfetch_reattests_completed_owner_artifact_when_upgrading_v1(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "provider"
    acquisition = FileBackedAcquisitionClient(
        tmp_path / "owner-artifacts",
        PROTOTYPE_FULLTEXT,
    )
    runner = SequencedPrototypeRunner([PROTOTYPE_ACQUIRE, PROTOTYPE_FINAL])
    adapter = CodexDeepFetchAdapter(
        workspace,
        model_ref="gpt-test",
        acquisition_client=acquisition,
        process_runner=runner,
    )
    expected = _execute(adapter)
    checkpoint_path = next(workspace.glob("runs/*/private/protocol.json"))
    legacy = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    legacy["schema_ref"] = "meta-research/deepfetch-v4-protocol-checkpoint/v1"
    del legacy["acquisition_item_proofs"]
    checkpoint_path.write_text(
        json.dumps(legacy, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )

    restarted = CodexDeepFetchAdapter(
        workspace,
        model_ref="gpt-test",
        acquisition_client=acquisition,
        process_runner=runner,
    )
    replayed = _execute(restarted)

    assert replayed == expected
    assert acquisition.acquire_calls == [("acquisition_session_1", "acq-v4-1")]
    assert acquisition.query_calls == [
        ("acquisition_session_1", "acq-v4-1"),
        ("acquisition_session_1", "acq-v4-1"),
        ("acquisition_session_1", "acq-v4-1"),
    ]
    upgraded = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    assert upgraded["schema_ref"] == (
        "meta-research/deepfetch-v4-protocol-checkpoint/v2"
    )
    assert upgraded["acquisition_item_proofs"][0]["sha256"] == (
        hashlib.sha256(PROTOTYPE_FULLTEXT).hexdigest()
    )


def test_codex_deepfetch_blocks_v1_obtained_item_without_owner_frozen_proof(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "provider"
    acquisition = FileBackedAcquisitionClient(
        tmp_path / "owner-artifacts",
        PROTOTYPE_FULLTEXT,
    )
    runner = SequencedPrototypeRunner([PROTOTYPE_ACQUIRE, PROTOTYPE_FINAL])
    adapter = CodexDeepFetchAdapter(
        workspace,
        model_ref="gpt-test",
        acquisition_client=acquisition,
        process_runner=runner,
    )
    _execute(adapter)
    checkpoint_path = next(workspace.glob("runs/*/private/protocol.json"))
    legacy = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    legacy["schema_ref"] = "meta-research/deepfetch-v4-protocol-checkpoint/v1"
    del legacy["acquisition_item_proofs"]
    checkpoint_path.write_text(
        json.dumps(legacy, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    identity = ("acquisition_session_1", "acq-v4-1")
    execution = acquisition.executions[identity]
    acquisition.executions[identity] = replace(
        execution,
        results=tuple(
            replace(item, content_sha256=None, content_bytes=None)
            for item in execution.results
        ),
    )

    replayed = CodexDeepFetchAdapter(
        workspace,
        model_ref="gpt-test",
        acquisition_client=acquisition,
        process_runner=runner,
    )
    for _attempt in range(2):
        with pytest.raises(
            DeepFetchUnavailable,
            match="deepfetch_acquisition_owner_proof_legacy_missing",
        ) as blocked:
            _execute(replayed)
        assert blocked.value.durable_outcome == "terminal"

    assert acquisition.acquire_calls == [("acquisition_session_1", "acq-v4-1")]
    assert json.loads(checkpoint_path.read_text(encoding="utf-8"))["schema_ref"] == (
        "meta-research/deepfetch-v4-protocol-checkpoint/v1"
    )


def test_codex_deepfetch_does_not_upgrade_v1_from_corrupted_owner_identity(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "provider"
    initial_acquisition = FileBackedAcquisitionClient(
        tmp_path / "owner-artifacts",
        PROTOTYPE_FULLTEXT,
    )
    runner = SequencedPrototypeRunner([PROTOTYPE_ACQUIRE, PROTOTYPE_FINAL])
    adapter = CodexDeepFetchAdapter(
        workspace,
        model_ref="gpt-test",
        acquisition_client=initial_acquisition,
        process_runner=runner,
    )
    _execute(adapter)
    checkpoint_path = next(workspace.glob("runs/*/private/protocol.json"))
    legacy = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    legacy["schema_ref"] = "meta-research/deepfetch-v4-protocol-checkpoint/v1"
    del legacy["acquisition_item_proofs"]
    checkpoint_path.write_text(
        json.dumps(legacy, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    corrupt_owner = CorruptedOwnerIdentityAcquisitionClient(
        tmp_path / "owner-artifacts",
        PROTOTYPE_FULLTEXT,
    )

    with pytest.raises(
        DeepFetchUnavailable,
        match="^deepfetch_acquisition_reattestation_required$",
    ) as blocked:
        restarted = CodexDeepFetchAdapter(
            workspace,
            model_ref="gpt-test",
            acquisition_client=corrupt_owner,
            process_runner=runner,
        )
        _execute(restarted)

    assert blocked.value.durable_outcome == "pending"
    assert json.loads(checkpoint_path.read_text(encoding="utf-8"))["schema_ref"] == (
        "meta-research/deepfetch-v4-protocol-checkpoint/v1"
    )
    assert corrupt_owner.acquire_calls == []
    assert corrupt_owner.query_calls == [
        ("acquisition_session_1", "acq-v4-1")
    ]
    assert len(runner.calls) == 3


def test_codex_deepfetch_rejects_checkpoint_proof_not_reissued_by_owner(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "provider"
    acquisition = FileBackedAcquisitionClient(
        tmp_path / "owner-artifacts",
        PROTOTYPE_FULLTEXT,
    )
    runner = SequencedPrototypeRunner([PROTOTYPE_ACQUIRE, PROTOTYPE_FINAL])
    adapter = CodexDeepFetchAdapter(
        workspace,
        model_ref="gpt-test",
        acquisition_client=acquisition,
        process_runner=runner,
    )
    _execute(adapter)
    checkpoint_path = next(workspace.glob("runs/*/private/protocol.json"))
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    public_fulltext = next(workspace.glob("runs/*/public/fulltext/*.html"))
    checkpoint["acquisition_item_proofs"][0]["path"] = str(
        public_fulltext.resolve()
    )
    checkpoint_path.write_text(
        json.dumps(
            checkpoint,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )

    with pytest.raises(
        DeepFetchUnavailable,
        match="deepfetch_hosted_acquisition_proof_mismatch",
    ):
        restarted = CodexDeepFetchAdapter(
            workspace,
            model_ref="gpt-test",
            acquisition_client=acquisition,
            process_runner=runner,
        )
        _execute(restarted)

    assert acquisition.acquire_calls == [("acquisition_session_1", "acq-v4-1")]
    assert acquisition.query_calls == [
        ("acquisition_session_1", "acq-v4-1"),
        ("acquisition_session_1", "acq-v4-1"),
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
    acquisition = RecordingAcquisitionClient(tmp_path / "owner-artifacts")
    workspace = tmp_path / "provider"
    adapter = CodexDeepFetchAdapter(
        workspace,
        model_ref="gpt-test",
        acquisition_client=acquisition,
        process_runner=runner,
    )

    result = _execute(adapter)

    assert result.completion == "complete"
    assert len(runner.calls) == 14
    assert [call[1].request_id for call in acquisition.calls] == [
        f"acq-v4-{generation}" for generation in range(1, 13)
    ]

    restarted = CodexDeepFetchAdapter(
        workspace,
        model_ref="gpt-test",
        acquisition_client=acquisition,
        process_runner=runner,
    )
    replayed = _execute(restarted)

    assert replayed == result
    assert len(runner.calls) == 14
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
        acquisition_client=RecordingAcquisitionClient(
            tmp_path / "owner-artifacts"
        ),
        process_runner=runner,
    )
    root_job_ref = "deepfetch-logical-operation"

    _execute(adapter, replace(_request(), job_ref=root_job_ref))
    adapter.cancel_job(root_job_ref)
    adapter.finish_job(root_job_ref)

    expected_jobs = {
        root_job_ref,
        *(f"{root_job_ref}:v4-turn:{turn}" for turn in range(14)),
    }
    assert set(runner.cancelled_jobs) == expected_jobs
    assert set(runner.finished_jobs) == expected_jobs
    assert adapter.reconcile_cancelled_job(root_job_ref) is True


def test_codex_deepfetch_cancel_uses_signed_durable_reconciliation_first(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class DurableCancellationRunner:
        def __init__(self) -> None:
            self.cancelled_jobs: list[str] = []

        def run_durable_job(self, *_args: object, **_kwargs: object) -> None:
            raise AssertionError("provider execution is not part of this test")

        def cancel_job(self, job_ref: str) -> None:
            self.cancelled_jobs.append(job_ref)

    runner = DurableCancellationRunner()
    adapter = CodexDeepFetchAdapter(
        tmp_path / "provider",
        process_runner=runner,  # type: ignore[arg-type]
    )
    reconciled: list[str] = []
    monkeypatch.setattr(
        adapter,
        "reconcile_cancelled_job",
        lambda job_ref: reconciled.append(job_ref) is None,
    )

    adapter.cancel_job("deepfetch-logical-operation")

    assert reconciled == ["deepfetch-logical-operation"]
    assert runner.cancelled_jobs == []


def test_codex_deepfetch_has_no_hidden_durable_segment_generation_limit(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "provider"
    runner = DurableSegmentSequenceRunner(workspace, stopped_segments=8)
    adapter = CodexDeepFetchAdapter(workspace, process_runner=runner)

    result = _execute(
        adapter,
        replace(_request(), job_ref="deepfetch-many-segments"),
    )

    assert result.completion == "honest_empty"
    assert result.native_session_ref == "native-many-durable-segments"
    assert len(runner.calls) == 10
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
    acquisition = WaitingThenReadyAcquisitionClient(
        tmp_path / "owner-artifacts"
    )
    adapter = CodexDeepFetchAdapter(
        tmp_path / "provider",
        acquisition_client=acquisition,
        process_runner=runner,
    )

    with pytest.raises(DeepFetchUnavailable) as interrupted:
        _execute(adapter)

    assert interrupted.value.code == "deepfetch_acquisition_waiting_user"
    assert interrupted.value.durable_outcome == "pending"
    assert len(runner.calls) == 2
    checkpoint_path = next((tmp_path / "provider" / "runs").glob("*/private/protocol.json"))
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    assert checkpoint["phase"] == "pending_acquisition"
    assert checkpoint["pending_acquisition"]["request_id"] == "acq-v4-1"

    result = _execute(adapter)

    assert result.completion == "complete"
    assert len(runner.calls) == 3
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

    replayed = _execute(adapter)
    assert replayed.completion == "complete"
    assert len(runner.calls) == 3
    assert len(acquisition.calls) == 2


def test_codex_deepfetch_rejects_reader_output_when_native_trace_and_ledger_are_unavailable(
    tmp_path: Path,
) -> None:
    runner = SequencedPrototypeRunner(
        [PROTOTYPE_ACQUIRE, PROTOTYPE_FINAL],
    )
    runner.emit_reader_evidence = False
    adapter = CodexDeepFetchAdapter(
        tmp_path / "provider",
        acquisition_client=RecordingAcquisitionClient(
            tmp_path / "owner-artifacts"
        ),
        process_runner=runner,
    )

    with pytest.raises(DeepFetchUnavailable) as failure:
        _execute(adapter)

    assert failure.value.code == "deepfetch_reader_agent_trace_invalid"


def test_codex_deepfetch_accepts_reader_output_from_verified_codex_ledgers(
    tmp_path: Path,
) -> None:
    final = copy.deepcopy(PROTOTYPE_FINAL)
    workflow = final["workflow"]
    assert isinstance(workflow, dict)
    assignments = workflow["reader_assignments"]
    assert isinstance(assignments, list)
    assignments[0]["reader_agent_ref"] = "/root/reader_agent_1"
    runner = SequencedPrototypeRunner([PROTOTYPE_ACQUIRE, final])
    runner.emit_reader_evidence = False
    ledger = RecordedCodexReaderLedger()
    adapter = CodexDeepFetchAdapter(
        tmp_path / "provider",
        acquisition_client=RecordingAcquisitionClient(
            tmp_path / "owner-artifacts"
        ),
        process_runner=runner,
        codex_ledger_reader=ledger,
    )
    request = replace(_request(), runtime_binding=adapter.runtime_binding())
    ledger.expected_cwd = str(
        adapter._agent_workspace_path(
            f"{request.run_ref}:direct",
            canonical_hash(request.runtime_binding.as_dict()),
        )
    )

    result = adapter.execute(request)

    assert result.completion == "complete"
    assert result.papers_ledger is not None


def test_codex_deepfetch_accepts_terminal_delivery_after_reader_progress_message(
    tmp_path: Path,
) -> None:
    class ProgressThenFinalLedger(RecordedCodexReaderLedger):
        def read(self, session_ref: str) -> tuple[dict[str, object], ...]:
            records = super().read(session_ref)
            if session_ref != self.root_ref:
                return records
            progress = {
                "type": "response_item",
                "payload": {
                    "type": "agent_message",
                    "author": self.task_name,
                    "recipient": "/root",
                    "content": [
                        {
                            "type": "input_text",
                            "text": (
                                "Message Type: MESSAGE\n"
                                f"Task name: {self.task_name}\n"
                                f"Sender: {self.task_name}\n"
                                "Payload:\n"
                            ),
                        }
                    ],
                },
            }
            return (*records[:-1], progress, records[-1])

    final = copy.deepcopy(PROTOTYPE_FINAL)
    workflow = final["workflow"]
    assert isinstance(workflow, dict)
    assignments = workflow["reader_assignments"]
    assert isinstance(assignments, list)
    assignments[0]["reader_agent_ref"] = "/root/reader_agent_1"
    ledger = ProgressThenFinalLedger()
    adapter = CodexDeepFetchAdapter(
        tmp_path / "provider",
        acquisition_client=RecordingAcquisitionClient(
            tmp_path / "owner-artifacts"
        ),
        process_runner=SequencedPrototypeRunner([PROTOTYPE_ACQUIRE, final]),
        codex_ledger_reader=ledger,
    )
    request = replace(_request(), runtime_binding=adapter.runtime_binding())
    ledger.expected_cwd = str(
        adapter._agent_workspace_path(
            f"{request.run_ref}:direct",
            canonical_hash(request.runtime_binding.as_dict()),
        )
    )

    result = adapter.execute(request)

    assert result.completion == "complete"
    assert result.papers_ledger is not None


def test_codex_deepfetch_rejects_nonterminal_last_reader_delivery(
    tmp_path: Path,
) -> None:
    class FinalThenProgressLedger(RecordedCodexReaderLedger):
        def read(self, session_ref: str) -> tuple[dict[str, object], ...]:
            records = super().read(session_ref)
            if session_ref != self.root_ref:
                return records
            progress = {
                "type": "response_item",
                "payload": {
                    "type": "agent_message",
                    "author": self.task_name,
                    "recipient": "/root",
                    "content": [
                        {
                            "type": "input_text",
                            "text": "Message Type: MESSAGE\nPayload:\nstill working",
                        }
                    ],
                },
            }
            return (*records, progress)

    ledger = FinalThenProgressLedger()
    ledger.expected_cwd = str(tmp_path)

    with pytest.raises(DeepFetchUnavailable) as failure:
        _verified_codex_reader_ledger_refs(
            ledger,
            root_session_ref=ledger.root_ref,
            expected_working_directory=ledger.expected_cwd,
        )

    assert failure.value.code == "deepfetch_reader_agent_trace_invalid"


def test_codex_deepfetch_configured_ledger_failure_does_not_fall_back_to_stdout(
    tmp_path: Path,
) -> None:
    class MissingLedger(RecordedCodexReaderLedger):
        def read(self, session_ref: str) -> tuple[dict[str, object], ...]:
            raise OSError(f"missing {session_ref}")

    adapter = CodexDeepFetchAdapter(
        tmp_path / "provider",
        acquisition_client=RecordingAcquisitionClient(
            tmp_path / "owner-artifacts"
        ),
        process_runner=SequencedPrototypeRunner(
            [PROTOTYPE_ACQUIRE, PROTOTYPE_FINAL]
        ),
        codex_ledger_reader=MissingLedger(),
    )

    with pytest.raises(DeepFetchUnavailable) as failure:
        _execute(adapter)

    assert failure.value.code == "deepfetch_reader_agent_trace_invalid"


def test_codex_deepfetch_rejects_same_count_different_stdout_and_ledger_child(
    tmp_path: Path,
) -> None:
    class DifferentChildRunner(SequencedPrototypeRunner):
        def __call__(
            self, argv: list[str], prompt: str, timeout: float | None
        ) -> subprocess.CompletedProcess[str]:
            completed = super().__call__(argv, prompt, timeout)
            events = [
                json.loads(line)
                for line in completed.stdout.splitlines()
                if line.strip()
            ]
            for event in events:
                item = event.get("item")
                if (
                    not isinstance(item, dict)
                    or item.get("type") != "collab_tool_call"
                ):
                    continue
                receivers = item.get("receiver_thread_ids")
                states = item.get("agents_states")
                if not isinstance(receivers, list) or not isinstance(states, dict):
                    continue
                item["receiver_thread_ids"] = ["native-different-child"]
                item["agents_states"] = {
                    "native-different-child": next(iter(states.values()))
                }
            return subprocess.CompletedProcess(
                argv,
                completed.returncode,
                stdout="\n".join(json.dumps(event) for event in events),
                stderr=completed.stderr,
            )

    final = copy.deepcopy(PROTOTYPE_FINAL)
    workflow = final["workflow"]
    assert isinstance(workflow, dict)
    assignments = workflow["reader_assignments"]
    assert isinstance(assignments, list)
    assignments[0]["reader_agent_ref"] = "/root/reader_agent_1"
    ledger = RecordedCodexReaderLedger()
    adapter = CodexDeepFetchAdapter(
        tmp_path / "provider",
        acquisition_client=RecordingAcquisitionClient(
            tmp_path / "owner-artifacts"
        ),
        process_runner=DifferentChildRunner(
            [PROTOTYPE_ACQUIRE, final]
        ),
        codex_ledger_reader=ledger,
    )
    request = replace(_request(), runtime_binding=adapter.runtime_binding())
    ledger.expected_cwd = str(
        adapter._agent_workspace_path(
            f"{request.run_ref}:direct",
            canonical_hash(request.runtime_binding.as_dict()),
        )
    )

    with pytest.raises(DeepFetchUnavailable) as failure:
        adapter.execute(request)

    assert failure.value.code == "deepfetch_reader_agent_trace_invalid"


def test_codex_deepfetch_rejects_malformed_native_agent_trace(
    tmp_path: Path,
) -> None:
    runner = MalformedReaderTraceRunner(
        [PROTOTYPE_ACQUIRE, PROTOTYPE_FINAL],
    )
    adapter = CodexDeepFetchAdapter(
        tmp_path / "provider",
        acquisition_client=RecordingAcquisitionClient(
            tmp_path / "owner-artifacts"
        ),
        process_runner=runner,
    )

    with pytest.raises(DeepFetchUnavailable) as failure:
        _execute(adapter)

    assert failure.value.code == "deepfetch_reader_agent_trace_invalid"


def test_codex_deepfetch_accepts_completed_close_agent_reader_evidence(
    tmp_path: Path,
) -> None:
    runner = SequencedPrototypeRunner(
        [PROTOTYPE_ACQUIRE, PROTOTYPE_FINAL],
        terminal_reader_tool="close_agent",
    )
    adapter = CodexDeepFetchAdapter(
        tmp_path / "provider",
        acquisition_client=RecordingAcquisitionClient(
            tmp_path / "owner-artifacts"
        ),
        process_runner=runner,
    )

    result = _execute(adapter)

    assert result.completion == "complete"
    assert result.papers_ledger is not None


def test_codex_deepfetch_accepts_visible_reader_name_with_verified_native_child(
    tmp_path: Path,
) -> None:
    runner = NativeReaderIdentityRunner(
        [PROTOTYPE_ACQUIRE, PROTOTYPE_FINAL],
    )
    adapter = CodexDeepFetchAdapter(
        tmp_path / "provider",
        acquisition_client=RecordingAcquisitionClient(
            tmp_path / "owner-artifacts"
        ),
        process_runner=runner,
    )

    result = _execute(adapter)

    assert result.completion == "complete"
    assert result.papers_ledger is not None


def test_codex_deepfetch_rejects_reader_count_mismatch_when_trace_is_available(
    tmp_path: Path,
) -> None:
    runner = AdditionalNativeReaderRunner(
        [PROTOTYPE_ACQUIRE, PROTOTYPE_FINAL],
    )
    adapter = CodexDeepFetchAdapter(
        tmp_path / "provider",
        acquisition_client=RecordingAcquisitionClient(
            tmp_path / "owner-artifacts"
        ),
        process_runner=runner,
    )

    with pytest.raises(DeepFetchUnavailable) as failure:
        _execute(adapter)

    assert failure.value.code == "deepfetch_reader_agent_trace_invalid"


def test_codex_deepfetch_rejects_duplicate_visible_reader_names(
    tmp_path: Path,
) -> None:
    final = copy.deepcopy(PROTOTYPE_FINAL)
    workflow = final["workflow"]
    assert isinstance(workflow, dict)
    assignments = workflow["reader_assignments"]
    assert isinstance(assignments, list)
    assignments.append(
        {
            "paper_id": "doi:10.1000/second",
            "assignment_id": "reader-assignment-2",
            "reader_agent_ref": "reader-agent-1",
            "status": "complete",
        }
    )
    runner = SequencedPrototypeRunner([PROTOTYPE_ACQUIRE, final])
    runner.emit_reader_evidence = False
    adapter = CodexDeepFetchAdapter(
        tmp_path / "provider",
        acquisition_client=RecordingAcquisitionClient(
            tmp_path / "owner-artifacts"
        ),
        process_runner=runner,
    )

    with pytest.raises(DeepFetchUnavailable) as failure:
        _execute(adapter)

    assert failure.value.code == "deepfetch_reader_agent_trace_invalid"


def test_codex_deepfetch_deduplicates_wait_and_close_for_one_reader(
    tmp_path: Path,
) -> None:
    runner = SequencedPrototypeRunner(
        [PROTOTYPE_ACQUIRE, PROTOTYPE_FINAL],
        emit_close_after_wait=True,
    )
    adapter = CodexDeepFetchAdapter(
        tmp_path / "provider",
        acquisition_client=RecordingAcquisitionClient(
            tmp_path / "owner-artifacts"
        ),
        process_runner=runner,
    )

    result = _execute(adapter)

    assert result.completion == "complete"


def test_codex_deepfetch_uses_live_web_in_a_dedicated_full_access_root_session(
    tmp_path: Path,
) -> None:
    runner = RecordingRunner(PROTOTYPE_EMPTY_FINAL)
    adapter = CodexDeepFetchAdapter(
        tmp_path / "provider", process_runner=runner
    )

    result = _execute(adapter)

    assert result.completion == "honest_empty"
    assert result.native_session_ref == "native-web-research-1"
    assert result.web_evidence is not None
    assert result.web_evidence["search_event_count"] == 2
    assert result.web_evidence["fetch_event_count"] == 2
    gate_argv, gate_prompt, gate_timeout = runner.calls[0]
    argv, prompt, timeout = runner.calls[1]
    assert argv[:2] == ["codex", "exec"]
    assert "--ignore-user-config" not in argv
    assert "--strict-config" in argv
    assert argv[argv.index("--sandbox") + 1] == "danger-full-access"
    config_values = [
        argv[index + 1] for index, value in enumerate(argv) if value == "--config"
    ]
    assert "mcp_servers={}" not in config_values
    assert 'approval_policy="never"' in config_values
    assert 'model_reasoning_effort="max"' in config_values
    assert 'web_search="live"' in config_values
    assert "features.multi_agent=true" in config_values
    assert argv[argv.index("--model") + 1] == "gpt-5.6-sol"
    assert gate_argv[-1] == "-"
    assert gate_timeout is None
    assert "web_evidence_gate=v1" in gate_prompt
    assert "Codex 默认工具能力保持可用" in gate_prompt
    assert "禁止使用 shell" not in gate_prompt
    assert gate_argv[gate_argv.index("--model") + 1] == "gpt-5.6-sol"
    gate_config_values = [
        gate_argv[index + 1]
        for index, value in enumerate(gate_argv)
        if value == "--config"
    ]
    assert 'model_reasoning_effort="max"' in gate_config_values
    assert "mcp_servers={}" not in gate_config_values
    assert argv[-3:] == ["resume", result.native_session_ref, "-"]
    assert "draft_revision=3" in prompt
    assert "不得把 Cookie、凭据、浏览器 profile" in prompt
    assert timeout is None
    schema = runner.schemas[1]
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(PROTOTYPE_EMPTY_FINAL)
    assert adapter.runtime_binding().capability_bindings[-2:] == (
        "web-fetch-live",
        "web-search-live",
    )
    assert (
        "codex-config:model_reasoning_effort=max"
        in adapter.runtime_binding().capability_bindings
    )
    assert (
        "codex-default-capabilities:v1"
        in adapter.runtime_binding().capability_bindings
    )


def test_codex_deepfetch_does_not_require_user_namespaces_on_the_deployed_host(
    tmp_path: Path,
) -> None:
    runner = NamespaceRestrictedRunner(PROTOTYPE_EMPTY_FINAL)
    adapter = CodexDeepFetchAdapter(
        tmp_path / "provider", model_ref="gpt-test", process_runner=runner
    )

    result = _execute(adapter)

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
        _execute(adapter)


def test_codex_deepfetch_fails_typed_when_live_provider_is_unavailable(
    tmp_path: Path,
) -> None:
    def unavailable(
        argv: list[str], prompt: str, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("codex")

    adapter = CodexDeepFetchAdapter(tmp_path / "provider", process_runner=unavailable)
    with pytest.raises(DeepFetchUnavailable, match="codex_cli_unavailable"):
        _execute(adapter)


def test_codex_deepfetch_rejects_model_output_without_real_web_tool_events(
    tmp_path: Path,
) -> None:
    runner = WebEvidenceGateRunner(PROTOTYPE_EMPTY_FINAL, gate_fetch=False)
    adapter = CodexDeepFetchAdapter(tmp_path / "provider", process_runner=runner)

    with pytest.raises(DeepFetchUnavailable, match="deepfetch_web_evidence_invalid"):
        _execute(adapter)


def test_codex_deepfetch_web_gate_rejects_search_without_fetch_before_acquisition(
    tmp_path: Path,
) -> None:
    runner = WebEvidenceGateRunner(PROTOTYPE_ACQUIRE, gate_fetch=False)
    acquisition = RecordingAcquisitionClient(tmp_path / "owner-artifacts")
    adapter = CodexDeepFetchAdapter(
        tmp_path / "provider",
        acquisition_client=acquisition,
        process_runner=runner,
    )

    with pytest.raises(DeepFetchUnavailable) as failure:
        _execute(adapter)

    assert failure.value.code == "deepfetch_web_evidence_invalid"
    assert len(runner.calls) == 1
    assert acquisition.calls == []
    assert not list((tmp_path / "provider").glob("runs/*/public/papers.json"))


def test_codex_deepfetch_web_gate_resumes_the_same_native_session(
    tmp_path: Path,
) -> None:
    runner = WebEvidenceGateRunner(PROTOTYPE_EMPTY_FINAL, gate_fetch=True)
    adapter = CodexDeepFetchAdapter(
        tmp_path / "provider",
        process_runner=runner,
    )

    result = _execute(adapter)

    assert result.completion == "honest_empty"
    assert len(runner.calls) == 2
    gate_argv, gate_prompt, gate_timeout = runner.calls[0]
    full_argv, full_prompt, full_timeout = runner.calls[1]
    assert gate_argv[-1] == "-"
    assert gate_timeout is None
    assert "web_evidence_gate=v1" in gate_prompt
    assert full_argv[-3:] == ["resume", result.native_session_ref, "-"]
    assert "deepfetch_skill_root=" in full_prompt
    assert full_timeout is None
    assert result.web_evidence is not None
    assert result.web_evidence["search_event_count"] == 2
    assert result.web_evidence["fetch_event_count"] == 2


def test_codex_deepfetch_rejects_a_changed_native_session_on_resume(
    tmp_path: Path,
) -> None:
    class ChangedSessionRunner(WebEvidenceGateRunner):
        def __call__(
            self, argv: list[str], prompt: str, timeout: float | None
        ) -> subprocess.CompletedProcess[str]:
            completed = super().__call__(argv, prompt, timeout)
            if "web_evidence_gate=v1" in prompt:
                return completed
            return subprocess.CompletedProcess(
                argv,
                completed.returncode,
                stdout=completed.stdout.replace(
                    "native-web-research-1", "native-web-research-2"
                ),
                stderr=completed.stderr,
            )

    runner = ChangedSessionRunner(PROTOTYPE_EMPTY_FINAL, gate_fetch=True)
    adapter = CodexDeepFetchAdapter(
        tmp_path / "provider", process_runner=runner
    )

    with pytest.raises(DeepFetchUnavailable) as failure:
        _execute(adapter)

    assert failure.value.code == "deepfetch_native_session_changed"
    assert len(runner.calls) == 2
    assert runner.calls[1][0][-3:] == [
        "resume",
        "native-web-research-1",
        "-",
    ]


def test_codex_deepfetch_accepts_the_real_open_ref_web_event_shape(
    tmp_path: Path,
) -> None:
    runner = WebEvidenceGateRunner(PROTOTYPE_EMPTY_FINAL, gate_fetch=True)
    adapter = CodexDeepFetchAdapter(tmp_path / "provider", process_runner=runner)

    result = _execute(adapter)

    assert result.web_evidence is not None
    assert result.web_evidence["search_event_count"] == 2
    assert result.web_evidence["fetch_event_count"] == 2


def test_codex_deepfetch_rejects_a_generic_other_event_as_fetch_evidence(
    tmp_path: Path,
) -> None:
    runner = WebEvidenceGateRunner(
        PROTOTYPE_EMPTY_FINAL,
        gate_fetch=True,
        gate_fetch_query="not-a-fetch",
    )
    adapter = CodexDeepFetchAdapter(tmp_path / "provider", process_runner=runner)

    with pytest.raises(DeepFetchUnavailable, match="deepfetch_web_evidence_invalid"):
        _execute(adapter)


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
import signal
import sys
import time

arguments = sys.argv[1:]
pathlib.Path(__file__).with_suffix('.argv').write_text(
    json.dumps(arguments), encoding='utf-8')
result_path = pathlib.Path(arguments[arguments.index('--output-last-message') + 1])
prompt = sys.stdin.read()
thread_ref = 'native-web-research-durable'
print(json.dumps({'type': 'thread.started', 'thread_id': thread_ref}), flush=True)
pathlib.Path(__file__).with_suffix('.session-started').write_text(
    thread_ref, encoding='utf-8')
if 'resume' not in arguments:
    signal.signal(signal.SIGTERM, lambda *_args: None)
    time.sleep(30)
else:
    print(json.dumps({'type': 'item.completed', 'item': {
        'id': 'search-durable', 'type': 'web_search', 'query': 'paper',
        'action': {'type': 'search'}}}), flush=True)
    print(json.dumps({'type': 'item.completed', 'item': {
        'id': 'fetch-durable', 'type': 'web_search',
        'query': 'https://example.org/paper', 'action': {'type': 'other'}}}),
        flush=True)
    if 'web_evidence_gate=v1' in prompt:
        result_path.write_text(
            json.dumps({'status': 'web_evidence_ready'}), encoding='utf-8')
        raise SystemExit(0)
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
    first = CodexDeepFetchAdapter(
        workspace,
        executable=str(executable),
    )
    request = replace(
        _request(),
        runtime_binding=first.runtime_binding(),
        job_ref="deepfetch-run:durable",
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
    receipts = list(
        workspace.glob(
            "provider-operations/*/deepfetch-initial/supervisor-exit.json"
        )
    )
    assert len(receipts) == 1
    _key_path, key = ensure_transport_key(workspace)
    assert read_transport_envelope(receipts[0], key)["termination_reason"] == (
        "stopped"
    )

    restarted = CodexDeepFetchAdapter(
        workspace,
        executable=str(executable),
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
    durable_argv = json.loads(
        executable.with_suffix(".argv").read_text(encoding="utf-8")
    )
    durable_config_values = [
        durable_argv[index + 1]
        for index, value in enumerate(durable_argv)
        if value == "--config"
    ]
    assert 'model_reasoning_effort="max"' in durable_config_values
    assert durable_argv[durable_argv.index("--model") + 1] == "gpt-5.6-sol"


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
if 'web_evidence_gate=v1' in prompt:
    result_path.write_text(
        json.dumps({'status': 'web_evidence_ready'}), encoding='utf-8')
    raise SystemExit(0)
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
    first = CodexDeepFetchAdapter(
        workspace,
        executable=str(executable),
        model_ref="gpt-test",
    )
    first_request = replace(
        _request(),
        runtime_binding=first.runtime_binding(),
        job_ref="deepfetch-run:early-stop:1",
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
    assert counter_path.read_text(encoding="utf-8") == "3"
