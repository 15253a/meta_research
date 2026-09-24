"""Startup reads accepted metadata; delivery and completion check file bytes."""
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import text

import meta_research.owners.research_memory as rm_module
from meta_research.owners.common import OwnerConflict
from meta_research.owners.research_memory import AssetIntakeRequest
from meta_research.owners.target_run_runtime import _FrozenInputFile
from meta_research.target_implementation_bundle import build_target_implementation_bundle
from test_target_input_asset_version_reference import _version_owner
from test_target_root_finalizer import _root_finalizer_fixture
from test_target_root_frozen_inputs import _fixture


@pytest.fixture
def registered_input(tmp_path):
    runtime, _lifecycle, _memory, _authority, handle, _root, _evidence = (
        _root_finalizer_fixture(tmp_path)
    )
    try:
        owner = runtime.owners.research_memory
        intake = owner.submit_asset_intake(AssetIntakeRequest(
            source_kind="text", custody_mode="managed", display_name="input.txt",
            media_type="text/plain", content=b"accepted research input\n" * 128,
        ), idempotency_key="startup-cost-asset")
        binding = intake.asset.as_binding()
        quest_ref = runtime.target_run_authorities.agent_runtime.query_target_workspace_quest_ref(handle)
        role = runtime.owners.research_graph.accept_asset_role(
            binding=binding, role="evidence", quest_ref=quest_ref,
            idempotency_key="startup-cost-role",
        )
        yield runtime, handle, binding, role
    finally:
        runtime.close()


@pytest.fixture
def accepted_input(registered_input):
    runtime, handle, binding, role = registered_input
    receipt = runtime.target_run_authorities.research_memory.accept_input_asset(
        target_ref=handle.target_ref, asset=binding,
        idempotency_key="startup-cost-input",
    )
    runtime.target_run_authorities.research_graph.accept_input_asset_role(
        target_ref=handle.target_ref, role=role, rm_proof_receipt=receipt,
        idempotency_key="startup-cost-input-role",
    )
    return runtime, handle, binding


def test_first_input_proof_binding_does_not_hash_accepted_assets(registered_input, monkeypatch):
    runtime, handle, binding, role = registered_input
    memory = runtime.target_run_authorities.research_memory
    graph = runtime.target_run_authorities.research_graph
    assert memory.query_input_asset(target_ref=handle.target_ref, asset_ref=binding.asset_ref) is None

    def reject_file_scan(*_args, **_kwargs):
        pytest.fail("first Target input binding rehashed an accepted upstream asset")

    monkeypatch.setattr(rm_module, "_sha256_exact_file", reject_file_scan)
    receipt = memory.accept_input_asset(target_ref=handle.target_ref, asset=binding,
        idempotency_key="first-startup-input")
    assert memory.accept_input_asset(target_ref=handle.target_ref, asset=binding,
        idempotency_key="first-startup-input") == receipt
    projection = graph.accept_input_asset_role(target_ref=handle.target_ref, role=role,
        rm_proof_receipt=receipt, idempotency_key="first-startup-input-role")
    assert projection.asset == binding
    assert graph.query_bundle_input_asset_proof(target_ref=handle.target_ref,
        asset_ref=binding.asset_ref).asset_ref == binding.asset_ref


def test_existing_input_proof_reads_never_hash_asset_files(accepted_input, monkeypatch):
    runtime, handle, binding = accepted_input

    def reject_file_scan(*_args, **_kwargs):
        pytest.fail("ordinary input proof query rehashed an accepted asset")

    monkeypatch.setattr(rm_module, "_sha256_exact_file", reject_file_scan)
    graph = runtime.target_run_authorities.research_graph
    for _ in range(3):
        proof = graph.query_bundle_input_asset_proof(
            target_ref=handle.target_ref, asset_ref=binding.asset_ref,
        )
        assert proof.asset_ref == binding.asset_ref
        assert proof.rm_acceptance_receipt.verified and proof.rg_role_receipt.verified
        described = runtime.target_run_authorities.research_memory.describe_input_asset(
            target_ref=handle.target_ref, asset_ref=binding.asset_ref,
        )
        assert described[0] == binding
        assert described[2].memory_ref == binding.version_ref


def test_input_proof_still_rejects_changed_exact_metadata(accepted_input):
    runtime, handle, binding = accepted_input
    with runtime._database.write() as connection:
        connection.execute(text(
            "UPDATE rm_target_input_asset_proofs SET content_hash=:hash "
            "WHERE target_ref=:target_ref AND asset_ref=:asset_ref"
        ), {"hash": "0" * 64, "target_ref": handle.target_ref, "asset_ref": binding.asset_ref})
    with pytest.raises(OwnerConflict):
        runtime.target_run_authorities.research_graph.query_bundle_input_asset_proof(
            target_ref=handle.target_ref, asset_ref=binding.asset_ref,
        )


def test_batch_resolution_visits_the_dependency_set_once():
    refs = tuple(f"asset_input_{index}" for index in range(32))
    owner, template, accepted, _issued = _version_owner(refs)
    roles = tuple(replace(template, asset_ref=ref, role_ref=f"role_{index}",
        version_ref=f"asset_version_{index}") for index, ref in enumerate(refs))
    for role in roles:
        accepted[role.asset_ref] = SimpleNamespace(asset=role.asset_binding(),
            source_role_ref=role.role_ref, source_role_receipt=role.receipt)
    calls = []

    def dependencies(target_ref):
        calls.append(target_ref)
        return "quest", tuple(role.asset_binding() for role in roles)

    owner._domain_reader.query_target_dependency_asset_bindings = dependencies
    owner._domain_reader.query_asset_roles = lambda **kw: tuple(
        role for role in roles if role.version_ref in kw["version_refs"]
    )
    assert owner.resolve_input_asset_refs(target_ref="target", input_refs=refs) == refs
    assert calls == ["target"]
    with pytest.raises(OwnerConflict, match="target_input_reference_not_declared"):
        owner.resolve_input_asset_refs(target_ref="target", input_refs=("asset_foreign",))


def _streamed_workspace(tmp_path, accepted_input):
    runtime, _handle, binding = accepted_input
    handle, frozen, workspace, *_rest = _fixture(tmp_path / "downstream")
    workspace._memory = runtime.target_run_authorities.research_memory
    description = workspace._memory.describe_asset_export(binding.version_ref)
    artifact = replace(frozen.artifacts[0], content=None,
        version_ref=binding.version_ref, content_hash=binding.content_hash,
        tree_hash=binding.content_hash, export_description=description)
    return handle, replace(frozen, artifacts=(artifact,)), workspace


def test_delivery_and_resume_do_not_rescan_but_completion_detects_tampering(
    tmp_path, accepted_input, monkeypatch,
):
    handle, frozen, workspace = _streamed_workspace(tmp_path, accepted_input)
    original = workspace._input_file_matches

    def no_payload_scan(path, expected):
        if isinstance(expected, _FrozenInputFile):
            pytest.fail("startup rescanned bytes already checked while exporting")
        return original(path, expected)

    with monkeypatch.context() as scoped:
        scoped.setattr(workspace, "_input_file_matches", no_payload_scan)
        # The tree verifier is a classmethod, so patch its class seam too.
        scoped.setattr(type(workspace), "_input_file_matches", staticmethod(no_payload_scan))
        paths = workspace.materialize_target_workspace_inputs(
            handle=handle, accepted_target_commit_inputs=(frozen,),
        )
        assert workspace.materialize_target_workspace_inputs(
            handle=handle, accepted_target_commit_inputs=(frozen,),
        ) == paths
    workspace.verify_target_workspace_inputs(handle=handle, accepted_target_commit_inputs=(frozen,))
    payload = next(Path(path) for path in paths if "/artifacts/" in path)
    payload.chmod(0o600)
    content = payload.read_bytes()
    payload.write_bytes(b"!" + content[1:])
    with pytest.raises(OwnerConflict, match="target_run_workspace_input_integrity_invalid"):
        workspace.verify_target_workspace_inputs(handle=handle, accepted_target_commit_inputs=(frozen,))


def test_streamed_export_still_checks_the_actual_source_bytes(tmp_path, accepted_input):
    runtime, _handle, binding = accepted_input
    handle, frozen, workspace = _streamed_workspace(tmp_path, accepted_input)
    source = runtime.owners.research_memory._object_store / rm_module._managed_asset_object_path(binding.content_hash)
    content = source.read_bytes()
    source.write_bytes(b"!" + content[1:])
    with pytest.raises(OwnerConflict, match="target_run_workspace_input_integrity_invalid"):
        workspace.materialize_target_workspace_inputs(handle=handle, accepted_target_commit_inputs=(frozen,))


def test_accepted_directory_archive_keeps_zip_delivery(tmp_path, accepted_input):
    runtime, _handle, _binding = accepted_input
    bundle = build_target_implementation_bundle((("train.py", 0o644, b"print('research')\n"),))
    intake = runtime.owners.research_memory.submit_asset_intake(AssetIntakeRequest(
        source_kind="text", custody_mode="managed", display_name="implementation.zip",
        media_type="application/zip", content=bundle.bundle_bytes,
    ), idempotency_key="startup-archive")
    binding = intake.asset.as_binding()
    handle, frozen, workspace, *_rest = _fixture(tmp_path / "archive-downstream")
    workspace._memory = runtime.target_run_authorities.research_memory
    description = workspace._memory.describe_asset_export(binding.version_ref)
    artifact = replace(frozen.artifacts[0], content=None, artifact_kind="directory",
        media_type="application/zip", version_ref=binding.version_ref,
        content_hash=binding.content_hash, tree_hash=bundle.tree_sha256,
        export_description=description)
    frozen = replace(frozen, artifacts=(artifact,))
    paths = workspace.materialize_target_workspace_inputs(
        handle=handle, accepted_target_commit_inputs=(frozen,),
    )
    delivered = next(Path(path) for path in paths if "/artifacts/" in path)
    assert delivered.suffix == ".zip"
    assert delivered.read_bytes() == bundle.bundle_bytes
    workspace.verify_target_workspace_inputs(handle=handle, accepted_target_commit_inputs=(frozen,))
