from __future__ import annotations

import subprocess

import pytest

from meta_research.harness_adapters import CodexHarnessAdapter
from test_harness_adapters import _RecordedRunner
from test_target_launch_admission import _ready_launch
from test_target_root_finalizer import _current_bundle_runtime


class _TargetDelivered(BaseException):
    pass


class _TargetResearchReader:
    """Provider stub that uses the actual Target resident MCP grant."""

    def __init__(self) -> None:
        self.runtime = None
        self.read_operations: list[str] = []
        self.probes = _RecordedRunner("codex", ())

    def __call__(
        self,
        argv: list[str],
        prompt: str,
        timeout: float | None,
        environment: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        if "--cd" not in argv:
            return self.probes(argv, prompt, timeout, environment or {})
        assert self.runtime is not None
        assert environment is not None
        token = environment["META_RESEARCH_MCP_TOKEN"]
        session_id = None

        def rpc(method: str, params: dict[str, object]) -> dict[str, object]:
            nonlocal session_id
            status, response, session_id = self.runtime.harnesses.dispatch_mcp_http(
                token,
                {"jsonrpc": "2.0", "id": method, "method": method, "params": params},
                mcp_session_id=session_id,
            )
            assert status == 200, response
            assert response is not None and "error" not in response, response
            return response["result"]

        rpc(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "target-rg-rm-read-test", "version": "1"},
            },
        )
        status, response, session_id = self.runtime.harnesses.dispatch_mcp_http(
            token,
            {
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
                "params": {},
            },
            mcp_session_id=session_id,
        )
        assert status == 202 and response is None
        listed = rpc("tools/list", {})
        assert {item["name"] for item in listed["tools"]} >= {
            "tools.catalog.page",
            "tools.contracts.read",
            "tools.call",
        }
        operation_ids = set()
        catalog_hash = None
        offset = 0
        while True:
            arguments = {
                "view": "operations",
                "query": "research_",
                "offset": offset,
                "limit": 2,
            }
            if catalog_hash is not None:
                arguments["catalog_hash"] = catalog_hash
            catalog = rpc(
                "tools/call",
                {"name": "tools.catalog.page", "arguments": arguments},
            )
            assert not catalog.get("isError"), catalog
            page = catalog["structuredContent"]
            operation_ids.update(item["operation_id"] for item in page["items"])
            catalog_hash = page["catalog_hash"]
            next_offset = page["next_offset"]
            if next_offset is None:
                break
            offset = next_offset
        assert {
            "research_graph.questions.page",
            "research_memory.assets.page",
        } <= operation_ids

        for operation_id in (
            "research_graph.questions.page",
            "research_memory.assets.page",
            "research_graph.question_relations.read",
            "research_memory.literature.page",
        ):
            contract = rpc(
                "tools/call",
                {
                    "name": "tools.contracts.read",
                    "arguments": {"operation_id": operation_id},
                },
            )
            assert not contract.get("isError"), (operation_id, contract)
            read = rpc(
                "tools/call",
                {
                    "name": "tools.call",
                    "arguments": {"operation_id": operation_id, "arguments": {}},
                },
            )
            assert not read.get("isError"), (operation_id, read)
            self.read_operations.append(operation_id)

        questions = rpc(
            "tools/call",
            {
                "name": "tools.call",
                "arguments": {
                    "operation_id": "research_graph.questions.page",
                    "arguments": {"query": "", "offset": 0, "limit": 1},
                },
            },
        )
        assert not questions.get("isError"), questions
        items = questions["structuredContent"].get("items", [])
        if items:
            question_ref = items[0].get("question_ref")
            if isinstance(question_ref, str) and question_ref:
                history = rpc(
                    "tools/call",
                    {
                        "name": "tools.call",
                        "arguments": {
                            "operation_id": "research_graph.question_history.read",
                            "arguments": {"question_ref": question_ref, "offset": 0, "limit": 1},
                        },
                    },
                )
                assert not history.get("isError"), history
                self.read_operations.append("research_graph.question_history.read")

        raise _TargetDelivered()


def test_target_provider_can_read_rg_and_rm_through_its_resident_channel(tmp_path):
    reader = _TargetResearchReader()
    adapter = CodexHarnessAdapter(tmp_path / "native-harness", runner=reader)
    runtime = _current_bundle_runtime(
        tmp_path / "runtime", harness_adapters=(adapter,)
    )
    reader.runtime = runtime
    try:
        _graph, target, _bundle, dispatch, launch = _ready_launch(runtime)
        runtime.owners.agent_runtime.admit_target_launch(
            launch,
            dispatch_decision_ref=dispatch.decision_ref,
            idempotency_key="target-rg-rm-read",
        )
        runtime.configure_resident_mcp_endpoint("http://127.0.0.1:8765")
        runtime.target_run_runtime.configure_resident_mcp_endpoint(
            "http://127.0.0.1:8765"
        )
        with pytest.raises(_TargetDelivered):
            for _ in range(16):
                runtime.target_run_runtime.process_once(target.target_ref)
        assert reader.read_operations == [
            "research_graph.questions.page",
            "research_memory.assets.page",
            "research_graph.question_relations.read",
            "research_memory.literature.page",
            "research_graph.question_history.read",
        ]
    finally:
        runtime.close()
