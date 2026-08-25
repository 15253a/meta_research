from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from meta_research import cli
from meta_research.composition import build_production_runtime
from meta_research.harness import (
    FULL_CONFORMANCE_OPERATION_IDS,
    FULL_CONFORMANCE_V1,
    FullConformanceRequest,
    HarnessAdmissionError,
    HarnessProbeRequest,
)
from meta_research.harness_adapters import (
    CLAUDE_LOCKED_VERSION,
    CODEX_LOCKED_VERSION,
    HARNESS_CAPABILITIES,
    HarnessInvocation,
    HarnessTurnEvidence,
)
from meta_research.owners.common import canonical_hash
from meta_research.paths import prepare_data_root
from meta_research.web import create_app


class _FullConformanceAdapter:
    def __init__(
        self, family: str, *, missing_capability: str | None = None
    ) -> None:
        self.family = family
        self.locked_version = (
            CODEX_LOCKED_VERSION
            if family == "codex"
            else CLAUDE_LOCKED_VERSION
        )
        self._missing_capability = missing_capability
        self.invocations: list[HarnessInvocation] = []

    def installation_profile(self) -> dict[str, object]:
        return {
            "harness_family": self.family,
            "locked_version": self.locked_version,
            "provider_version": self.locked_version,
            "status": "ready",
        }

    def invoke(self, invocation: HarnessInvocation) -> HarnessTurnEvidence:
        self.invocations.append(invocation)
        native_session_ref = (
            invocation.native_session_ref or f"{self.family}-native-session"
        )
        evidence_events = tuple(
            {
                "event_ref": (
                    "harness_evidence:"
                    + canonical_hash(
                        {
                            "operation_ref": invocation.provider_operation_ref,
                            "capability": capability,
                        }
                    )
                ),
                "sequence": sequence,
                "kind": f"observed:{capability}",
            }
            for sequence, capability in enumerate(HARNESS_CAPABILITIES, start=1)
        )
        capabilities: dict[str, object] = {}
        for sequence, capability in enumerate(HARNESS_CAPABILITIES, start=1):
            available = capability != self._missing_capability and (
                capability != "resume"
                or invocation.native_session_ref is not None
            )
            capabilities[capability] = (
                {
                    "status": "available",
                    "evidence_refs": [evidence_events[sequence - 1]["event_ref"]],
                }
                if available
                else {
                    "status": "capability_unavailable",
                    "reason": {"code": "probe_evidence_missing"},
                    "evidence_refs": [],
                }
            )
        profile = {
            "schema_ref": "meta-research/harness-capability-profile/v1",
            "harness_family": self.family,
            "locked_version": self.locked_version,
            "provider_version": self.locked_version,
            "native_session_ref": native_session_ref,
            "capabilities": capabilities,
        }
        return HarnessTurnEvidence(
            native_session_ref=native_session_ref,
            profile=profile,
            evidence_events=evidence_events,
            stream_hash=canonical_hash(evidence_events),
        )


def _runtime(tmp_path: Path, *, codex_missing: str | None = None):
    data_root = prepare_data_root(tmp_path)
    codex = _FullConformanceAdapter(
        "codex", missing_capability=codex_missing
    )
    claude = _FullConformanceAdapter("claude")
    runtime = build_production_runtime(
        data_root,
        harness_adapters=(codex, claude),
    )
    return runtime, codex, claude


def _full_request() -> FullConformanceRequest:
    return FullConformanceRequest(
        codex_model_ref="gpt-conformance",
        codex_auth_profile_ref="harness-profile:codex-default",
        claude_model_ref="claude-conformance",
        claude_auth_profile_ref="harness-profile:claude-default",
    )


def test_old_partial_probe_can_never_make_product_harness_ready(
    tmp_path: Path,
) -> None:
    runtime, _codex, _claude = _runtime(tmp_path / "partial")
    try:
        admission = runtime.harnesses.admit_probe(
            HarnessProbeRequest(
                request_ref="legacy-partial-probe",
                harness_family="codex",
                model_ref="gpt-partial",
                auth_profile_ref="harness-profile:codex-default",
                required_operation_ids=("research_graph.snapshot.read",),
                required_capabilities=("native_session", "stream"),
            ),
            idempotency_key="legacy-partial-probe",
        )
        runtime.harnesses.execute_probe(
            admission.run.request_ref,
            prompt="Run a legacy bounded subset probe.",
            mcp_base_url="http://127.0.0.1:8765",
        )

        status = runtime.harnesses.query_status()
        assert status["status"] == "capability_unavailable"
        assert status["conformance"]["conformance_ref"] is None
        assert all(
            adapter["status"] == "capability_unavailable"
            and adapter["capability_profile"] is None
            for adapter in status["adapters"]
        )
        assert status["adapters"][0]["missing_reason"] == {
            "code": "full_conformance_not_recorded"
        }
    finally:
        runtime.close()


def test_only_one_completed_family_is_not_ready_then_both_are_ready(
    tmp_path: Path,
) -> None:
    runtime, codex, claude = _runtime(tmp_path / "complete")
    try:
        admitted = runtime.harnesses.start_full_conformance(_full_request())
        assert admitted.contract_ref == FULL_CONFORMANCE_V1
        assert len(admitted.contract_hash) == 64
        assert {run.harness_family for run in admitted.runs} == {
            "codex",
            "claude",
        }
        assert all(
            admitted.contract_hash in run.request_ref for run in admitted.runs
        )
        assert all(
            [
                binding["semantic_operation_id"]
                for binding in run.mcp_binding.operation_bindings
            ]
            == list(FULL_CONFORMANCE_OPERATION_IDS)
            for run in admitted.runs
        )

        assert runtime.harnesses.advance_full_conformance(
            mcp_base_url="http://127.0.0.1:8765"
        )
        assert runtime.harnesses.advance_full_conformance(
            mcp_base_url="http://127.0.0.1:8765"
        )
        one_family = runtime.harnesses.query_status()
        assert one_family["status"] == "capability_unavailable"
        assert [item["status"] for item in one_family["adapters"]] == [
            "ready",
            "capability_unavailable",
        ]
        assert len(codex.invocations) == 2
        assert len(claude.invocations) == 0

        assert runtime.harnesses.advance_full_conformance(
            mcp_base_url="http://127.0.0.1:8765"
        )
        assert runtime.harnesses.advance_full_conformance(
            mcp_base_url="http://127.0.0.1:8765"
        )
        complete = runtime.harnesses.query_status()
        assert complete["status"] == "ready"
        assert all(item["status"] == "ready" for item in complete["adapters"])
        assert complete["conformance"] == {
            "contract_ref": FULL_CONFORMANCE_V1,
            "contract_hash": admitted.contract_hash,
            "conformance_ref": admitted.conformance_ref,
            "required_families": ["codex", "claude"],
            "required_capabilities": list(HARNESS_CAPABILITIES),
            "required_operation_ids": list(FULL_CONFORMANCE_OPERATION_IDS),
        }
        binding = runtime.harnesses.require_full_conformance_binding()
        assert binding.contract_ref == FULL_CONFORMANCE_V1
        assert binding.contract_hash == admitted.contract_hash
        assert binding.conformance_ref == admitted.conformance_ref
        assert len(binding.profile_receipts) == 2
        assert all(
            receipt.startswith(
                f"harness-artifact:full-conformance-profile:{family}:"
            )
            for family, receipt in zip(
                ("codex", "claude"), binding.profile_receipts, strict=True
            )
        )
        assert len(binding.binding_hash) == 64
        assert len(claude.invocations) == 2
    finally:
        runtime.close()


def test_one_missing_capability_keeps_full_matrix_unavailable(
    tmp_path: Path,
) -> None:
    runtime, _codex, _claude = _runtime(
        tmp_path / "missing", codex_missing="web_fetch"
    )
    try:
        runtime.harnesses.start_full_conformance(_full_request())
        with pytest.raises(
            HarnessAdmissionError,
            match="required_harness_capability_unavailable",
        ):
            runtime.harnesses.advance_full_conformance(
                mcp_base_url="http://127.0.0.1:8765"
            )
        assert runtime.harnesses.advance_full_conformance(
            mcp_base_url="http://127.0.0.1:8765"
        )
        assert runtime.harnesses.advance_full_conformance(
            mcp_base_url="http://127.0.0.1:8765"
        )

        status = runtime.harnesses.query_status()
        assert status["status"] == "capability_unavailable"
        assert status["adapters"][0]["status"] == "capability_unavailable"
        assert status["adapters"][0]["missing_reason"] == {
            "code": "required_harness_capability_unavailable"
        }
        assert status["adapters"][1]["status"] == "ready"
    finally:
        runtime.close()


def test_cli_conformance_start_posts_only_provider_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    data_root = prepare_data_root(tmp_path / "cli")
    state = object()
    captured: dict[str, object] = {}

    monkeypatch.setattr(cli, "_require_running", lambda _data_root: state)

    def internal_request(
        actual_data_root,
        actual_state,
        path,
        *,
        method="POST",
        payload=None,
    ):
        captured.update(
            {
                "data_root": actual_data_root,
                "state": actual_state,
                "path": path,
                "method": method,
                "payload": payload,
            }
        )
        return {
            "status": "admitted",
            "conformance_ref": "hfc_cli",
            "contract_ref": FULL_CONFORMANCE_V1,
            "contract_hash": "a" * 64,
            "runs": [],
        }

    monkeypatch.setattr(cli, "_internal_request", internal_request)

    assert (
        cli.main(
            [
                "conformance",
                "start",
                "--data-root",
                str(data_root.root),
                "--codex-model",
                "gpt-conformance",
                "--claude-model",
                "claude-conformance",
                "--json",
            ]
        )
        == 0
    )
    assert captured["path"] == "/internal/harness-conformance"
    assert captured["method"] == "POST"
    assert captured["payload"] == {
        "codex_model_ref": "gpt-conformance",
        "codex_auth_profile_ref": "harness-profile:codex-default",
        "claude_model_ref": "claude-conformance",
        "claude_auth_profile_ref": "harness-profile:claude-default",
    }
    assert "hfc_cli" in capsys.readouterr().out


def test_internal_product_route_admits_the_fixed_two_family_set(
    tmp_path: Path,
) -> None:
    runtime, _codex, _claude = _runtime(tmp_path / "internal-route")
    base_url = "http://127.0.0.1:8765"
    client = TestClient(
        create_app(
            runtime,
            base_url=base_url,
            control_key="control-secret",
        ),
        base_url=base_url,
    )
    try:
        response = client.post(
            "/internal/harness-conformance",
            headers={"X-Meta-Research-Control": "control-secret"},
            json={
                "codex_model_ref": "gpt-conformance",
                "codex_auth_profile_ref": "harness-profile:codex-default",
                "claude_model_ref": "claude-conformance",
                "claude_auth_profile_ref": "harness-profile:claude-default",
            },
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["contract_ref"] == FULL_CONFORMANCE_V1
        assert [run["harness_family"] for run in payload["runs"]] == [
            "codex",
            "claude",
        ]
        assert runtime.harnesses.query_status()["status"] == (
            "capability_unavailable"
        )
        with client:
            deadline = time.monotonic() + 2
            while (
                runtime.harnesses.query_status()["status"] != "ready"
                and time.monotonic() < deadline
            ):
                time.sleep(0.02)
        assert runtime.harnesses.query_status()["status"] == "ready"
    finally:
        client.close()
        runtime.close()
