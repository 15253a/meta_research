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
                argv, 0, "codex-cli 0.156.1\n", ""
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
            model_ref="gpt-6-sol",
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


@pytest.mark.parametrize("root_kind", ROOT_AGENT_KINDS)
def test_each_root_actual_adapter_uses_its_effective_catalog_without_tool_ritual(
    tmp_path: Path,
    root_kind: RootAgentKind,
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
    assert len(operation_ids) == len(set(operation_ids))
    assert "research_memory.content.read" in operation_ids
    assert {
        "human_request.read",
        "research_graph.baselines.page",
        "research_graph.baselines.read",
        "research_graph.target_formal_results.read",
        "research_graph.datasets.derive",
        "research_graph.datasets.derive.reconcile",
    }.issubset(operation_ids)
    assert ("research_memory.research_notes.read" in operation_ids) == (
        root_kind in {"idea", "plan", "bundle", "reasoning", "target"}
    )
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


@pytest.mark.parametrize("root_kind", ROOT_AGENT_KINDS)
@pytest.mark.parametrize("entry_path", ROOT_CAPABILITY_ENTRY_PATHS)
def test_root_launch_uses_one_native_ultra_preset_and_effective_max(
    tmp_path: Path, root_kind: RootAgentKind, entry_path: RootCapabilityEntryPath,
) -> None:
    _, argv = _invoke_root_without_tool_activity(
        tmp_path, root_kind=root_kind, entry_path=entry_path,
    )
    assert argv[argv.index("--model") + 1] == "gpt-6-sol"
    assert [x for x in argv if x.startswith("model_reasoning_effort=")] == [
        'model_reasoning_effort="ultra"'
    ]
    bindings = root_capability_profile(root_kind).runtime_bindings()
    assert "codex-config:model_reasoning_effort=ultra" in bindings
    assert "codex-effective:reasoning.effort=max" in bindings
    assert "codex-collaboration-mode:ultra" in bindings

def test_output_language_changes_next_root_launch_without_changing_binding(tmp_path):
    from meta_research.system_prompt import read_output_language
    (tmp_path / "data-root.json").write_text("{}")
    workspace = tmp_path / "run/workspace"
    workspace.mkdir(parents=True)
    profile = root_capability_profile("reasoning")
    binding = profile.digest
    (tmp_path / "user-preferences.json").write_text('{"output_language":"en"}')
    argv = profile.codex_arguments(output_language=read_output_language(workspace))
    assert "直接使用英文撰写" in " ".join(argv)
    (tmp_path / "user-preferences.json").write_text('{"output_language":"zh"}')
    argv = profile.codex_arguments(output_language=read_output_language(workspace))
    assert "直接使用中文撰写" in " ".join(argv)
    assert profile.digest == binding


@pytest.mark.parametrize("stage", ["idea", "plan", "bundle", "reasoning"])
@pytest.mark.parametrize("effective", ["max", "high", "ultra"])
def test_formal_root_binding_accepts_exact_effective_reasoning_identity(stage, effective):
    from meta_research.codex_runtime import CODEX_REASONING_EFFORT_BINDING
    from meta_research.owners.agent_runtime import (
        IdeaRuntimeBinding, PlanRuntimeBinding, BundleRuntimeBinding,
        ReasoningRuntimeBinding, _validated_runtime_binding,
    )
    kind = {"idea": IdeaRuntimeBinding, "plan": PlanRuntimeBinding,
        "bundle": BundleRuntimeBinding, "reasoning": ReasoningRuntimeBinding}[stage]
    binding = kind(
        packaged_skill_bundle_hash="1" * 64, instruction_set_hash="2" * 64,
        model_ref="gpt-6-sol", harness_adapter_ref="codex-cli/0.156.1",
        mcp_bindings=(
            "harness-operation-binding:semantic-mcp-catalog@sha256:" + "3" * 64,
            "harness-operation-binding:semantic-mcp-operation-bindings@sha256:" + "4" * 64,
        ),
        capability_bindings=("harness-operation-binding-v1", "semantic-mcp-resident"),
        resource_bindings=(
            "harness-artifact:operation-binding-contract:" + stage + "@sha256:" + "5" * 64,
            "harness-artifact:operation-binding-set:" + stage + "@sha256:" + "6" * 64,
            CODEX_REASONING_EFFORT_BINDING if effective == "max" else "codex-effective:reasoning.effort=" + effective,
        ),
    )
    if effective == "max":
        assert _validated_runtime_binding(binding, stage=stage)[0] == binding
    else:
        from meta_research.owners.common import OwnerConflict
        with pytest.raises(OwnerConflict, match="idea_runtime_binding_unauthorized"):
            _validated_runtime_binding(binding, stage=stage)
