"""Selected historical results authorize exact companion inputs across Cycles."""
from dataclasses import replace
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from meta_research.owners.common import OwnerConflict
from test_target_input_reference_bridge import _authority


def _companion_owner(refs=("asset_companion",), *, commits=None):
    owner = _authority(refs, plan=True)
    template = owner._domain_reader.query_asset_roles(version_refs=("version_1",))[0]
    role = replace(template, role_ref="role_companion", asset_ref="asset_companion",
        version_ref="asset_version_companion",
        asset_receipt=replace(template.asset_receipt, subject_ref="asset_version_companion"))
    assets = commits if commits is not None else {"commit_1": (role.asset_binding(),), "commit_2": ()}
    requests, accepted, issued = [], {}, []

    def read(*, quest_ref, target_commit_refs):
        assert quest_ref == "quest"
        requests.append(target_commit_refs)
        return {ref: assets.get(ref, ()) for ref in target_commit_refs}

    owner._domain_reader.query_target_commit_input_asset_bindings = read
    owner._domain_reader.accept_asset_role = lambda **kw: issued.append(kw) or role
    owner._memory.accept_input_asset = lambda **kw: role.asset_receipt
    owner.query_input_asset_projection = lambda **kw: accepted.get(kw["asset_ref"])

    def accept(**kw):
        with owner._database.read() as db:
            db.execute(text("INSERT INTO rg_target_input_asset_role_proofs VALUES (:target, :asset, :key)"),
                {"target": kw["target_ref"], "asset": kw["role"].asset_ref, "key": kw["idempotency_key"]})
            db.commit()
        accepted[role.asset_ref] = SimpleNamespace(asset=role.asset_binding(),
            source_role_ref=role.role_ref, source_role_receipt=role.receipt)
        return accepted[role.asset_ref]

    owner.accept_input_asset_role = accept
    return owner, role, accepted, issued, requests


@pytest.mark.parametrize("ref", ["asset_companion", "asset_version_companion"])
def test_selected_commit_companion_prepares_exact_proof_and_retains_lineage_on_replay(ref):
    owner, role, accepted, issued, requests = _companion_owner((ref,))
    assert owner.prepare_input_assets("target") is True
    assert accepted[role.asset_ref].asset == role.asset_binding()
    assert owner.resolve_input_asset_ref(target_ref="target", input_ref=ref) == role.asset_ref
    assert owner.selected_evidence_target_commits("target") == {"commit_1": (ref,)}
    assert owner.prepare_input_assets("target") is False
    assert owner.selected_evidence_target_commits("target") == {"commit_1": (ref,)}
    assert len(issued) == 1
    assert all(set(value) == {"commit_1", "commit_2"} for value in requests)


def test_later_target_graph_gets_its_own_proofs_for_the_same_frozen_companion():
    owner, role, _, issued, _ = _companion_owner()
    with owner._database.read() as db:
        db.execute(text("INSERT INTO rg_targets SELECT 'target_later', 'graph_later', "
            "spec_json, spec_hash FROM rg_targets WHERE target_ref = 'target'"))
        db.execute(text("INSERT INTO rg_target_graphs SELECT 'graph_later', quest_ref, "
            "formal_plan_ref, plan_content_ref, plan_document_hash, context_pack_ref, "
            "context_pack_hash, request_ref FROM rg_target_graphs WHERE graph_ref = 'graph'"))
        db.commit()
    authority_reader = owner._domain_reader.query_target_measurement_domain_authority
    owner._domain_reader.query_target_measurement_domain_authority = lambda target: SimpleNamespace(
        **{**vars(authority_reader(target)), "graph_ref": "graph_later" if target == "target_later" else "graph"}
    )
    projections, memory_targets = {}, []
    accept = owner.accept_input_asset_role
    def accept_scoped(**kw):
        projection = accept(**kw)
        projections[(kw["target_ref"], kw["role"].asset_ref)] = projection
        return projection
    owner.accept_input_asset_role = accept_scoped
    owner.query_input_asset_projection = lambda **kw: projections.get((kw["target_ref"], kw["asset_ref"]))
    owner._memory.accept_input_asset = lambda **kw: memory_targets.append(kw["target_ref"]) or role.asset_receipt

    assert owner.prepare_input_assets("target") is True
    assert owner.prepare_input_assets("target_later") is True
    assert memory_targets == ["target", "target_later"]
    assert len(issued) == 2
    assert set(projections) == {("target", role.asset_ref), ("target_later", role.asset_ref)}
    for target in ("target", "target_later"):
        assert projections[(target, role.asset_ref)].asset == role.asset_binding()
        assert owner.selected_evidence_target_commits(target) == {"commit_1": (role.asset_ref,)}
        assert owner.prepare_input_assets(target) is False
    with owner._database.read() as db:
        rows = db.execute(text("SELECT target_ref, idempotency_key FROM rg_target_input_asset_role_proofs")).all()
    assert len(rows) == 2 and len({row.idempotency_key for row in rows}) == 2


def test_unselected_commit_does_not_authorize_a_companion():
    owner, _, accepted, issued, _ = _companion_owner(commits={"unselected_commit": ()})
    with pytest.raises(OwnerConflict, match="target_input_asset_not_selected"):
        owner.prepare_input_assets("target")
    assert not accepted and not issued


def test_ambiguous_companion_asset_requires_an_exact_version():
    owner, role, accepted, issued, _ = _companion_owner()
    other = replace(role.asset_binding(), version_ref="asset_version_newer")
    owner._domain_reader.query_target_commit_input_asset_bindings = lambda **kw: {
        "commit_1": (role.asset_binding(),), "commit_2": (other,),
    }
    with pytest.raises(OwnerConflict, match="target_input_evidence_version_conflict"):
        owner.prepare_input_assets("target")
    assert not accepted and not issued


def test_exact_companion_version_ignores_a_newer_version_in_another_selected_commit():
    owner, role, accepted, issued, _ = _companion_owner(("asset_version_companion",))
    other = replace(role.asset_binding(), version_ref="asset_version_newer")
    owner._domain_reader.query_target_commit_input_asset_bindings = lambda **kw: {
        "commit_1": (role.asset_binding(),), "commit_2": (other,),
    }
    assert owner.resolve_input_asset_ref(
        target_ref="target", input_ref=role.version_ref,
    ) == role.asset_ref
    assert owner.selected_evidence_target_commits("target") == {"commit_1": (role.version_ref,)}
    assert not accepted and not issued  # Read paths cannot mint roles or proofs.
    assert owner.prepare_input_assets("target") is True
    assert accepted[role.asset_ref].asset == role.asset_binding()


def test_existing_legacy_direct_proof_does_not_gain_new_source_trees():
    owner, role, accepted, issued, requests = _companion_owner()
    accepted[role.asset_ref] = SimpleNamespace(asset=role.asset_binding(),
        source_role_ref=role.role_ref, source_role_receipt=role.receipt)
    assert owner.prepare_input_assets("target") is False
    assert owner.selected_evidence_target_commits("target") == {}
    assert not issued and not requests


def test_selected_companion_issuer_failure_is_not_turned_into_a_proof():
    owner, _, accepted, issued, _ = _companion_owner()
    def reject(**kw):
        raise OwnerConflict("target_input_dependency_manifest_invalid")
    owner._domain_reader.query_target_commit_input_asset_bindings = reject
    with pytest.raises(OwnerConflict, match="target_input_dependency_manifest_invalid"):
        owner.prepare_input_assets("target")
    assert not accepted and not issued
