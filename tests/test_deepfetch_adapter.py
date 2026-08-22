from __future__ import annotations

import json
import subprocess
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
    def __init__(self, output: dict[str, object]) -> None:
        self.output = output
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
        stdout = json.dumps(
            {"type": "thread.started", "thread_id": "native-web-research-1"}
        )
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
    argv, prompt, timeout = runner.calls[0]
    assert argv[:2] == ["codex", "exec"]
    assert "--ignore-user-config" in argv
    assert "--strict-config" in argv
    assert argv[argv.index("--sandbox") + 1] == "read-only"
    config_values = [
        argv[index + 1]
        for index, value in enumerate(argv)
        if value == "--config"
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

    adapter = CodexDeepFetchAdapter(
        tmp_path / "provider", process_runner=unavailable
    )
    with pytest.raises(DeepFetchUnavailable, match="codex_cli_unavailable"):
        adapter.execute(_request())
