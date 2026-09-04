from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from meta_research.harness_adapters import (
    HARNESS_CAPABILITIES,
    CodexHarnessAdapter,
    HarnessInvocation,
)
from meta_research.root_capabilities import (
    ROOT_AGENT_KINDS,
    ROOT_CAPABILITY_ENTRY_PATHS,
    ROOT_CAPABILITY_FLOOR,
    RootAgentKind,
    RootCapabilityEntryPath,
    root_capability_profile,
)
from meta_research.root_resident_mcp import RootResidentMcpChannels
from meta_research.semantic_owner_gateway import (
    ROOT_AGENT_SEMANTIC_OPERATION_IDS,
)


class _NoActivityCodexRunner:
    """Complete one real adapter invocation without inventing tool activity."""

    def __init__(self, session_ref: str) -> None:
        self.session_ref = session_ref
        self.calls: list[list[str]] = []

    def __call__(
        self,
        argv: list[str],
        prompt: str,
        timeout: float | None,
        environment: dict[str, str],
    ) -> subprocess.CompletedProcess[str]:
        del prompt, timeout, environment
        self.calls.append(list(argv))
        if "--version" in argv:
            return subprocess.CompletedProcess(
                argv, 0, "codex-cli 0.153.2\n", ""
            )
        if argv[-2:] == ["features", "list"]:
            # An unavailable optional inventory is diagnostic-only. Do not
            # fabricate provider feature output just to make the profile green.
            return subprocess.CompletedProcess(argv, 1, "", "")
        stream = (
            {"type": "thread.started", "thread_id": self.session_ref},
            {"type": "turn.completed", "thread_id": self.session_ref},
        )
        return subprocess.CompletedProcess(
            argv,
            0,
            "\n".join(json.dumps(event) for event in stream) + "\n",
            "",
        )


def _invoke_root_without_tool_activity(
    tmp_path: Path,
    *,
    root_kind: RootAgentKind,
    entry_path: RootCapabilityEntryPath = "initial",
) -> tuple[dict[str, object], list[str]]:
    session_ref = f"codex-{root_kind}-{entry_path}"
    runner = _NoActivityCodexRunner(session_ref)
    operation_ids = RootResidentMcpChannels(root_kind).operation_ids
    result = CodexHarnessAdapter(
        tmp_path / root_kind / entry_path,
        runner=runner,
    ).invoke(
        HarnessInvocation(
            harness_family="codex",
            provider_operation_ref=f"operation:{root_kind}:{entry_path}",
            run_ref=f"run:{root_kind}",
            attempt_ref=f"attempt:{root_kind}:{entry_path}",
            attempt_generation=1,
            root_session_ref=f"root-session:{root_kind}",
            fence_ref=f"fence:{root_kind}:{entry_path}",
            model_ref="gpt-test",
            prompt="Return the operation result without ceremonial tool calls.",
            mcp_url="http://127.0.0.1:0/mcp",
            mcp_token="test-operation-bearer",
            native_session_ref=(
                None if entry_path == "initial" else session_ref
            ),
            root_kind=root_kind,
            entry_path=entry_path,
            authorized_operation_ids=operation_ids,
        )
    )
    diagnostics = result.profile["root_capability_diagnostics"]
    assert isinstance(diagnostics, dict)
    return diagnostics, runner.calls[-1]


@pytest.mark.parametrize("root_kind", ROOT_AGENT_KINDS)
def test_every_root_uses_the_same_effective_capability_floor(root_kind: str) -> None:
    profile = root_capability_profile(root_kind)

    assert profile.capabilities == ROOT_CAPABILITY_FLOOR
    assert profile.capabilities == HARNESS_CAPABILITIES
    assert len(profile.capabilities) == len(set(profile.capabilities))
    assert profile.as_dict()["enabled_codex_features"] == [
        "multi_agent",
        "plugins",
        "remote_plugin",
        "hooks",
    ]

    invocations = {
        entry_path: profile.codex_arguments(entry_path=entry_path)
        for entry_path in ROOT_CAPABILITY_ENTRY_PATHS
    }
    assert len(set(invocations.values())) == 1
    argv = invocations["initial"]
    assert 'web_search="live"' in argv
    for feature in ("multi_agent", "plugins", "remote_plugin", "hooks"):
        assert ("--enable", feature) in tuple(zip(argv, argv[1:]))


@pytest.mark.parametrize("root_kind", ROOT_AGENT_KINDS)
def test_root_diagnostics_keep_availability_usage_and_authorization_separate(
    root_kind: str,
) -> None:
    profile = root_capability_profile(root_kind)
    diagnostics = profile.public_diagnostics(
        available_capabilities=("shell",),
        used_capabilities=(),
        authorized_operation_ids=("human_request.open",),
        tool_inventory_evidence_refs=("event:inventory",),
        tool_inventory_names=("shell", "mcp"),
    )

    assert diagnostics["root_kind"] == root_kind
    assert diagnostics["capability_profile_hash"] == profile.digest
    assert diagnostics["availability"]["shell"] == {"status": "available"}
    assert diagnostics["availability"]["plugin"] == {
        "status": "availability_not_observed"
    }
    assert diagnostics["usage"]["shell"] == {
        "status": "not_used",
        "evidence_refs": [],
    }
    assert diagnostics["side_effect_authorization"] == {
        "status": "operation_local",
        "operation_ids": ["human_request.open"],
    }
    assert diagnostics["tool_inventory"] == {
        "status": "observed",
        "evidence_refs": ["event:inventory"],
        "names": ["shell", "mcp"],
    }


def test_one_unsupported_capability_is_local_and_typed() -> None:
    profile = root_capability_profile("target")
    diagnostics = profile.public_diagnostics(
        available_capabilities=("shell",),
        unavailable_capabilities={"plugin": "harness_plugin_unsupported"},
    )

    assert diagnostics["availability"]["plugin"] == {
        "status": "capability_unavailable",
        "reason": {"code": "harness_plugin_unsupported"},
    }
    assert diagnostics["availability"]["shell"] == {"status": "available"}
    assert diagnostics["availability"]["web_fetch"] == {
        "status": "availability_not_observed"
    }
    assert diagnostics["usage"]["plugin"]["status"] == "not_used"


@pytest.mark.parametrize(
    ("root_kind", "operation_count"),
    (
        ("deepfetch", 4),
        ("acquisition", 4),
        ("companion", 4),
        ("idea", 4),
        ("plan", 4),
        ("bundle", 18),
        ("target", 5),
        ("reasoning", 7),
        ("writing", 4),
    ),
)
def test_each_root_actual_adapter_uses_its_effective_catalog_without_tool_ritual(
    tmp_path: Path,
    root_kind: RootAgentKind,
    operation_count: int,
) -> None:
    diagnostics, argv = _invoke_root_without_tool_activity(
        tmp_path,
        root_kind=root_kind,
    )
    profile = root_capability_profile(root_kind)
    operation_ids = ROOT_AGENT_SEMANTIC_OPERATION_IDS[root_kind]

    assert set(diagnostics["availability"]) == set(profile.capabilities)
    assert set(diagnostics["usage"]) == set(profile.capabilities)
    assert diagnostics["root_kind"] == root_kind
    assert diagnostics["capability_profile_hash"] == profile.digest
    assert diagnostics["side_effect_authorization"]["operation_ids"] == list(
        operation_ids
    )
    assert len(operation_ids) == operation_count
    assert diagnostics["tool_inventory"] == {
        "status": "not_reported",
        "evidence_refs": [],
        "names": [],
    }
    assert diagnostics["provider_feature_inventory"]["status"] == (
        "not_reported"
    )
    assert diagnostics["usage"]["subagent"] == {
        "status": "not_used",
        "evidence_refs": [],
    }
    assert diagnostics["usage"]["plugin"] == {
        "status": "not_used",
        "evidence_refs": [],
    }

    enabled_features = {
        argv[index + 1]
        for index, value in enumerate(argv[:-1])
        if value == "--enable"
    }
    configuration = {
        argv[index + 1]
        for index, value in enumerate(argv[:-1])
        if value == "--config"
    }
    assert enabled_features == {
        "hooks",
        "multi_agent",
        "plugins",
        "remote_plugin",
    }
    assert 'web_search="live"' in configuration
    assert "mcp_servers.meta_research.required=true" in configuration


@pytest.mark.parametrize("entry_path", ROOT_CAPABILITY_ENTRY_PATHS)
def test_every_entry_path_keeps_one_actual_root_floor_and_catalog(
    tmp_path: Path,
    entry_path: RootCapabilityEntryPath,
) -> None:
    diagnostics, _argv = _invoke_root_without_tool_activity(
        tmp_path,
        root_kind="bundle",
        entry_path=entry_path,
    )

    assert diagnostics["entry_path"] == entry_path
    assert diagnostics["capability_profile_hash"] == root_capability_profile(
        "bundle"
    ).digest
    assert diagnostics["side_effect_authorization"]["operation_ids"] == list(
        ROOT_AGENT_SEMANTIC_OPERATION_IDS["bundle"]
    )
    assert diagnostics["usage"]["subagent"]["status"] == "not_used"


def test_role_identity_does_not_change_the_canonical_floor_hash() -> None:
    assert len(
        {
            root_capability_profile(root_kind).digest
            for root_kind in ROOT_AGENT_KINDS
        }
    ) == 1
