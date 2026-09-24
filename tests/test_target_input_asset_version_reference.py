"""Exact upstream AssetVersions remain valid Target input references."""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from meta_research.owners.common import OwnerConflict
from test_target_input_reference_bridge import _authority


def _version_owner(refs=("asset_version_1",)):
    owner = _authority(refs, plan=True)
    role = owner._domain_reader.query_asset_roles(version_refs=("version_1",))[0]
    role = replace(role, version_ref="asset_version_1",
        asset_receipt=replace(role.asset_receipt, subject_ref="asset_version_1"))
    original_reader = owner._domain_reader
    original_reader.query_target_dependency_asset_bindings = lambda _: ("quest", (role.asset_binding(),))
    original_reader.query_asset_roles = lambda **kw: (role,) if role.version_ref in kw["version_refs"] else ()
    accepted = {}
    issued = []
    original_reader.accept_asset_role = lambda **kw: issued.append(kw) or role
    owner._memory.accept_input_asset = lambda **kw: role.asset_receipt
    owner.query_input_asset_projection = lambda **kw: accepted.get(kw["asset_ref"])
    def accept(**kw):
        accepted[role.asset_ref] = SimpleNamespace(asset=role.asset_binding(),
            source_role_ref=role.role_ref, source_role_receipt=role.receipt)
        return accepted[role.asset_ref]
    owner.accept_input_asset_role = accept
    return owner, role, accepted, issued


def test_dependency_version_prepares_exact_asset_proof_and_retains_source_alias():
    owner, role, accepted, issued = _version_owner()
    assert owner.prepare_input_assets("target") is True
    assert owner.resolve_input_asset_ref(target_ref="target", input_ref=role.version_ref) == role.asset_ref
    assert accepted[role.asset_ref].asset == role.asset_binding()
    assert issued[0]["binding"] == role.asset_binding()
    assert owner.input_asset_source_refs("target") == {role.asset_ref: (role.version_ref,)}
    assert owner.prepare_input_assets("target") is False
    assert len(issued) == 1


def test_dependency_asset_identity_prepares_the_commit_bound_version():
    owner, role, accepted, issued = _version_owner(("asset_same",))
    assert owner.prepare_input_assets("target") is True
    assert accepted[role.asset_ref].asset == role.asset_binding()
    assert owner.resolve_input_asset_ref(target_ref="target", input_ref=role.asset_ref) == role.asset_ref
    assert owner.prepare_input_assets("target") is False
    assert len(issued) == 1


def test_ambiguous_dependency_asset_identity_cannot_choose_a_version():
    owner, role, accepted, issued = _version_owner(("asset_same",))
    other = replace(role.asset_binding(), version_ref="asset_version_2")
    owner._domain_reader.query_target_dependency_asset_bindings = lambda _: ("quest", (role.asset_binding(), other))
    with pytest.raises(OwnerConflict, match="target_input_evidence_version_conflict"):
        owner.prepare_input_assets("target")
    assert not accepted and not issued


def test_unknown_version_cannot_issue_a_target_proof():
    owner, _role, accepted, issued = _version_owner(("asset_version_foreign",))
    with pytest.raises(OwnerConflict, match="target_input_asset_version_not_selected"):
        owner.prepare_input_assets("target")
    assert not accepted and not issued


def test_existing_other_version_does_not_replace_the_selected_dependency_version():
    owner, role, accepted, _issued = _version_owner()
    accepted[role.asset_ref] = SimpleNamespace(asset=replace(role.asset_binding(), version_ref="asset_version_other"),
        source_role_ref=role.role_ref, source_role_receipt=role.receipt)
    with pytest.raises(OwnerConflict, match="target_input_evidence_version_conflict"):
        owner.prepare_input_assets("target")


def test_dependency_issuer_rejection_cannot_be_turned_into_an_asset_role():
    owner, _role, accepted, issued = _version_owner()
    def reject(_):
        raise OwnerConflict("target_root_manifest_integrity_invalid")
    owner._domain_reader.query_target_dependency_asset_bindings = reject
    with pytest.raises(OwnerConflict, match="target_root_manifest_integrity_invalid"):
        owner.prepare_input_assets("target")
    assert not accepted and not issued


def test_two_dependency_versions_of_one_asset_cannot_share_one_input_proof():
    owner, role, accepted, issued = _version_owner(("asset_version_1", "asset_version_2"))
    other = replace(role.asset_binding(), version_ref="asset_version_2")
    owner._domain_reader.query_target_dependency_asset_bindings = lambda _: ("quest", (role.asset_binding(), other))
    with pytest.raises(OwnerConflict, match="target_input_evidence_version_conflict"):
        owner.prepare_input_assets("target")
    assert not accepted and not issued
