from __future__ import annotations

import json
import subprocess
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

from meta_research.deepfetch import (
    CodexDeepFetchAdapter,
    DeepFetchProviderRequest,
    DeepFetchRuntimeBinding,
    DeepFetchUnavailable,
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


class RecordingRunner:
    def __init__(
        self,
        output: dict[str, object],
        *,
        emit_web_evidence: bool = True,
        fetch_query: str = "https://example.org/paper",
    ) -> None:
        self.output = output
        self.emit_web_evidence = emit_web_evidence
        self.fetch_query = fetch_query
        self.calls: list[tuple[list[str], str, float]] = []
        self.schemas: list[dict[str, object]] = []

    def __call__(
        self, argv: list[str], prompt: str, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append((argv, prompt, timeout))
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
        stdout = "\n".join(json.dumps(event) for event in events)
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")


def _request() -> DeepFetchProviderRequest:
    return DeepFetchProviderRequest(
        request_ref="deepfetch_request_1",
        initialization_id="quest_init_1",
        correlation_ref="deepfetch_correlation_1",
        draft_revision=3,
        draft_hash="a" * 64,
        scope={"goal": "核查证据边界"},
        scope_hash="b" * 64,
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


def test_codex_deepfetch_uses_live_web_in_a_read_only_root_session(
    tmp_path: Path,
) -> None:
    runner = RecordingRunner(RESULT)
    adapter = CodexDeepFetchAdapter(
        tmp_path / "provider", model_ref="gpt-test", process_runner=runner
    )

    result = adapter.execute(_request())

    assert result.completion == "limited"
    assert result.native_session_ref == "native-web-research-1"
    assert result.web_evidence is not None
    assert result.web_evidence["search_event_count"] == 1
    assert result.web_evidence["fetch_event_count"] == 1
    argv, prompt, timeout = runner.calls[0]
    assert argv[:2] == ["codex", "exec"]
    assert "--ignore-user-config" in argv
    assert "--strict-config" in argv
    assert argv[argv.index("--sandbox") + 1] == "read-only"
    config_values = [
        argv[index + 1] for index, value in enumerate(argv) if value == "--config"
    ]
    assert "mcp_servers={}" in config_values
    assert 'approval_policy="never"' in config_values
    assert 'web_search="live"' in config_values
    assert argv[-1] == "-"
    assert "draft_revision=3" in prompt
    assert "不得输出 Cookie、凭据、浏览器状态" in prompt
    assert timeout > 0
    schema = runner.schemas[0]
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(RESULT)
    assert adapter.runtime_binding().capability_bindings[-2:] == (
        "web-fetch-live",
        "web-search-live",
    )


def test_codex_deepfetch_rejects_a_forged_receipt_field_even_if_runner_bypasses_schema(
    tmp_path: Path,
) -> None:
    runner = RecordingRunner({**RESULT, "receipt": {"status": "accepted"}})
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
    runner = RecordingRunner(RESULT, emit_web_evidence=False)
    adapter = CodexDeepFetchAdapter(tmp_path / "provider", process_runner=runner)

    with pytest.raises(DeepFetchUnavailable, match="deepfetch_web_evidence_invalid"):
        adapter.execute(_request())


def test_codex_deepfetch_accepts_the_real_open_ref_web_event_shape(
    tmp_path: Path,
) -> None:
    runner = RecordingRunner(RESULT, fetch_query="")
    adapter = CodexDeepFetchAdapter(tmp_path / "provider", process_runner=runner)

    result = adapter.execute(_request())

    assert result.web_evidence is not None
    assert result.web_evidence["search_event_count"] == 1
    assert result.web_evidence["fetch_event_count"] == 1


def test_codex_deepfetch_rejects_a_generic_other_event_as_fetch_evidence(
    tmp_path: Path,
) -> None:
    runner = RecordingRunner(RESULT, fetch_query="not-a-fetch")
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
def test_codex_deepfetch_rejects_semantically_impossible_results(
    tmp_path: Path,
    output: dict[str, object],
    code: str,
) -> None:
    adapter = CodexDeepFetchAdapter(
        tmp_path / "provider", process_runner=RecordingRunner(output)
    )

    with pytest.raises(DeepFetchUnavailable, match=code):
        adapter.execute(_request())


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
sys.stdin.read()
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
    result_path.write_text(json.dumps({result!r}), encoding='utf-8')
""".replace(
            "{result!r}", repr(RESULT)
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
sys.stdin.read()
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
result_path.write_text(json.dumps({result!r}), encoding='utf-8')
""".replace(
            "{result!r}", repr(RESULT)
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
