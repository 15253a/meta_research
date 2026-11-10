from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import threading
from dataclasses import replace

import pytest

from meta_research.root_resident_mcp import RootResidentMcpChannels, RootResidentMcpError
from meta_research.human_guidance import FrozenGuidanceBinding, GuidanceOperationIdentity
from test_external_root_resident_mcp import _authority, _scope


@pytest.mark.parametrize("root_kind", ["bundle", "reasoning"])
@pytest.mark.parametrize("cleanup", ["revoke", "release_job", "release_all"])
def test_terminal_cleanup_revokes_shared_grant_without_revoking_next_generation(root_kind, cleanup):
    channels = RootResidentMcpChannels(root_kind)
    authority = _authority(root_kind, "primary")
    template = authority.issue_resident_mcp_channel.return_value
    issued = []

    def issue(**values):
        grant = f"grant-{len(issued)}"
        channel = SimpleNamespace(
            connection=SimpleNamespace(token=f"token-{len(issued)}", grant_ref=grant),
            binding=SimpleNamespace(**{**vars(template.binding), "connection_grant_ref": grant}),
        )
        issued.append(channel)
        return channel

    authority.issue_resident_mcp_channel.side_effect = issue
    channels.bind_authority(authority)
    channels.configure_endpoint("http://127.0.0.1:8766")
    scope = _scope(root_kind)

    def acquire():
        return channels.acquire(
            run_ref=scope["run_ref"], attempt_ref=scope["attempt_ref"],
            root_session_ref=scope["root_session_ref"], fence_ref=scope["fence_ref"],
            capability_binding_hash=scope["runtime_binding_hash"],
            phase="primary", job_ref="same-physical-operation",
        )

    first_key, first_access = acquire()
    late_key, late_access = acquire()
    assert late_key == first_key and late_access.token == first_access.token
    assert len(issued) == 1
    if cleanup == "revoke":
        channels.revoke(first_key)
    elif cleanup == "release_job":
        channels.release_job("same-physical-operation")
    else:
        channels.release_all()
    authority.revoke_resident_mcp_channel.assert_called_once_with(issued[0])

    # The old concurrent resume is still unwinding when another caller obtains
    # a new grant for that exact same physical operation and scope.
    new_key, new_access = acquire()
    assert new_key != first_key and new_access.token != first_access.token
    release_late = threading.Event()
    with ThreadPoolExecutor(max_workers=1) as pool:
        def finish_late():
            assert release_late.wait(2)
            channels.release(late_key)
            channels.revoke(late_key)
        pending = pool.submit(finish_late)
        release_late.set()
        pending.result(timeout=2)
    authority.revoke_resident_mcp_channel.assert_called_once_with(issued[0])
    reused_key, reused_access = acquire()
    assert reused_key == new_key and reused_access.token == new_access.token
    assert len(issued) == 2
    channels.revoke(new_key)
    assert authority.revoke_resident_mcp_channel.call_args.args == (issued[1],)
    assert authority.revoke_resident_mcp_channel.call_count == 2
    channels.release(reused_key)
    assert authority.revoke_resident_mcp_channel.call_count == 2


@pytest.mark.parametrize("root_kind", ["bundle", "reasoning"])
@pytest.mark.parametrize("drift", ["endpoint", "catalog", "guidance"])
def test_changed_channel_authority_is_rejected_and_revoked(root_kind, drift):
    channels = RootResidentMcpChannels(root_kind)
    authority = _authority(root_kind, "primary")
    channel = authority.issue_resident_mcp_channel.return_value
    guidance = FrozenGuidanceBinding(GuidanceOperationIdentity(root_kind, "run", "operation"),
                                     "quest", "snapshot", "1" * 64)
    if drift == "endpoint":
        channel.binding.endpoint_ref = "//foreign-host/mcp"
    elif drift == "catalog":
        channel.binding.catalog_hash = "9" * 64
    else:
        channel.binding.guidance_binding = replace(guidance, snapshot_hash="2" * 64)
    channels.bind_authority(authority)
    channels.configure_endpoint("http://127.0.0.1:8765")
    with pytest.raises(RootResidentMcpError, match="semantic_mcp_endpoint_conflict"):
        channels.configure_endpoint("http://127.0.0.1:8766")
    with pytest.raises(RootResidentMcpError, match="semantic_mcp_channel_invalid"):
        channels.acquire(run_ref="run", attempt_ref="attempt", root_session_ref="root", fence_ref="fence",
            capability_binding_hash="3" * 64, phase="primary", job_ref="operation",
            guidance_binding=guidance if drift == "guidance" else None)
    authority.revoke_resident_mcp_channel.assert_called_once_with(channel)
