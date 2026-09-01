from __future__ import annotations

import pytest

from meta_research.harness_adapters import HARNESS_CAPABILITIES
from meta_research.root_capabilities import (
    ROOT_AGENT_KINDS,
    ROOT_CAPABILITY_ENTRY_PATHS,
    ROOT_CAPABILITY_FLOOR,
    codex_feature_diagnostics,
    root_capability_profile,
)


_CODEX_FEATURE_OUTPUT = """\
hooks stable true
multi_agent stable true
plugins stable true
remote_plugin stable true
shell_tool stable true
skill_search stable true
unified_exec stable true
"""


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
def test_every_root_projects_the_same_real_provider_feature_inventory(
    root_kind: str,
) -> None:
    profile = root_capability_profile(root_kind)
    diagnostics = codex_feature_diagnostics(
        profile=profile,
        entry_path="initial",
        provider_version="0.147.0",
        feature_output=_CODEX_FEATURE_OUTPUT,
    )

    assert diagnostics["availability"]["shell"]["status"] == "available"
    assert diagnostics["availability"]["plugin"]["status"] == "available"
    assert diagnostics["availability"]["skill"]["status"] == (
        "availability_not_observed"
    )
    assert diagnostics["usage"]["shell"]["status"] == "usage_not_observed"
    assert diagnostics["tool_inventory"]["status"] == "not_reported"
    assert diagnostics["provider_feature_inventory"]["status"] == "observed"
    assert diagnostics["provider_feature_inventory"]["evidence_refs"]


def test_role_identity_does_not_change_the_canonical_floor_hash() -> None:
    assert len(
        {
            root_capability_profile(root_kind).digest
            for root_kind in ROOT_AGENT_KINDS
        }
    ) == 1
