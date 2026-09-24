"""First Target input preparation reuses accepted custody without rehashing it."""

from dataclasses import replace

import pytest

import meta_research.owners.research_memory as rm_module
from meta_research.owners.common import OwnerConflict
from meta_research.owners.research_memory import AssetIntakeRequest
from test_target_root_finalizer import _root_finalizer_fixture


@pytest.fixture
def unprepared_input(tmp_path):
    runtime, _lifecycle, _memory, _authority, handle, _root, _evidence = (
        _root_finalizer_fixture(tmp_path)
    )
    try:
        intake = runtime.owners.research_memory.submit_asset_intake(
            AssetIntakeRequest(
                source_kind="text",
                custody_mode="managed",
                display_name="upstream-data.txt",
                media_type="text/plain",
                content=b"accepted upstream research data\n" * 128,
            ),
            idempotency_key="first-input-data",
        )
        binding = intake.asset.as_binding()
        quest_ref = runtime.target_run_authorities.agent_runtime.query_target_workspace_quest_ref(
            handle
        )
        yield runtime, handle, binding, quest_ref
    finally:
        runtime.close()


def test_first_prepare_issues_real_rm_and_rg_proofs_without_scanning_accepted_bytes(
    unprepared_input, monkeypatch
):
    runtime, handle, binding, quest_ref = unprepared_input
    graph = runtime.owners.research_graph
    authority = runtime.target_run_authorities.research_graph
    memory = runtime.target_run_authorities.research_memory
    assert graph.query_asset_roles(version_refs=(binding.version_ref,)) == ()
    assert memory.query_input_asset(
        target_ref=handle.target_ref, asset_ref=binding.asset_ref
    ) is None

    # Only the selected dependency is supplied by the fixture. Preparation,
    # role acceptance, both persisted proofs and every receipt read are real.
    monkeypatch.setattr(
        authority, "_declared_input_asset_refs", lambda _target: (binding.version_ref,)
    )
    monkeypatch.setattr(
        graph,
        "query_target_dependency_asset_bindings",
        lambda _target: (quest_ref, (binding,)),
    )

    def reject_scan(*_args, **_kwargs):
        pytest.fail("first Target input preparation rehashed accepted content")

    monkeypatch.setattr(rm_module, "_sha256_exact_file", reject_scan)
    monkeypatch.setattr(graph._asset_verifier, "verify_asset_binding", reject_scan)

    assert authority.prepare_input_assets(handle.target_ref) is True
    projection = authority.query_input_asset_projection(
        target_ref=handle.target_ref, asset_ref=binding.asset_ref
    )
    assert projection.asset == binding
    roles = graph.query_asset_roles(quest_ref=quest_ref, version_refs=(binding.version_ref,))
    assert len(roles) == 1
    assert roles[0].asset_binding() == binding
    assert roles[0].receipt == projection.source_role_receipt
    assert memory.query_input_asset(
        target_ref=handle.target_ref, asset_ref=binding.asset_ref
    ) == (binding, projection.rm_proof_receipt)
    assert authority.resolve_input_asset_refs(
        target_ref=handle.target_ref, input_refs=(binding.version_ref,)
    ) == (binding.asset_ref,)
    assert authority.prepare_input_assets(handle.target_ref) is False


def test_asset_role_default_still_checks_content(unprepared_input, monkeypatch):
    runtime, _handle, binding, quest_ref = unprepared_input
    graph = runtime.owners.research_graph
    verify = graph._asset_verifier.verify_asset_binding
    calls = []

    def verify_content(**values):
        calls.append(values["version_ref"])
        return verify(**values)

    monkeypatch.setattr(graph._asset_verifier, "verify_asset_binding", verify_content)
    role = graph.accept_asset_role(
        binding=binding,
        role="evidence",
        quest_ref=quest_ref,
        idempotency_key="ordinary-asset-role",
    )

    assert role.asset_binding() == binding
    assert calls == [binding.version_ref]


def test_metadata_only_role_still_rejects_a_forged_binding(unprepared_input, monkeypatch):
    runtime, _handle, binding, quest_ref = unprepared_input
    graph = runtime.owners.research_graph

    def reject_scan(*_args, **_kwargs):
        pytest.fail("metadata-only role attempted a content scan")

    monkeypatch.setattr(graph._asset_verifier, "verify_asset_binding", reject_scan)
    with pytest.raises(OwnerConflict, match="asset_receipt_invalid"):
        graph.accept_asset_role(
            binding=replace(binding, content_hash="0" * 64),
            role="evidence",
            quest_ref=quest_ref,
            idempotency_key="forged-input-role",
            verify_content=False,
        )

    assert graph.query_asset_roles(version_refs=(binding.version_ref,)) == ()
