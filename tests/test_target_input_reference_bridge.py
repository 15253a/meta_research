"""Boundary tests for the Target-scoped projection of verified Plan leaves.

The real issuer/bytes chain is separately exercised by the production-DB replay.
Here the domain reader is a trusted input so version-selection scope is isolated.
"""
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text

from meta_research.owners.common import (
    AcceptanceReceipt, AcceptedFormalPlanBinding, OwnerConflict, canonical_hash, canonical_json,
)
from meta_research.owners.research_graph import AcceptedAssetRole
from meta_research.owners.target_run_runtime import SQLiteTargetRunGraphAuthority


def _receipt(ref):
    return AcceptanceReceipt("research_graph", "test", "receipt_" + ref, ref, "a" * 64)


def _authority(refs, *, plan=False, existing=False):
    engine = create_engine("sqlite://")
    spec = {"candidate": {"direct_accepted_input_asset_refs": list(refs)}}
    with engine.begin() as db:
        db.execute(text("CREATE TABLE rg_targets(target_ref TEXT, graph_ref TEXT, spec_json TEXT, spec_hash TEXT)"))
        db.execute(text("CREATE TABLE rg_target_input_asset_role_proofs(target_ref TEXT, asset_ref TEXT, idempotency_key TEXT)"))
        db.execute(text("INSERT INTO rg_targets VALUES ('target', 'graph', :json, :hash)"),
                   {"json": canonical_json(spec), "hash": canonical_hash(spec)})
    owner = object.__new__(SQLiteTargetRunGraphAuthority)
    owner._database = SimpleNamespace(read=engine.connect)
    owner._memory = SimpleNamespace(query_input_asset=lambda **kw: None)
    owner.query_input_asset_projection = lambda **kw: object() if existing else None
    if not plan:
        # There are deliberately no graph/Plan tables or domain-reader methods.
        owner._domain_reader = SimpleNamespace(query_target_dependency_asset_bindings=lambda _: ("quest", ()))
        return owner

    roles = tuple(AcceptedAssetRole(
        role_ref=f"role_{n}", version_ref=f"version_{n}", asset_ref="asset_same",
        asset_hash=str(n) * 64, manifest_hash="f" * 64, role="evidence", quest_ref="quest",
        accepted_at=1.0, asset_receipt=_receipt(f"version_{n}"), receipt=_receipt(f"role_{n}"),
    ) for n in (1, 2))
    catalog = [{
        "evidence_ref": f"evidence_{n}", "asset_ref": role.asset_ref,
        "asset_version_ref": role.version_ref, "content_hash": role.asset_hash,
        "manifest_hash": role.manifest_hash, "role_ref": role.role_ref,
        "asset_receipt": role.asset_receipt.as_public_dict(),
        "role_receipt": role.receipt.as_public_dict(), "target_commit_root_ref": f"commit_{n}",
    } for n, role in enumerate(roles, 1)]
    leaves = tuple(SimpleNamespace(role="MetricResult", evidence_ref=entry["evidence_ref"],
        evidence_catalog_entry_hash=canonical_hash(entry), target_commit_ref=entry["target_commit_root_ref"])
        for entry in catalog)
    binding = AcceptedFormalPlanBinding("plan", "content", "b" * 64, "c" * 64,
        _receipt("content"), _receipt("plan"), "stage_commit", _receipt("stage_commit"), {})
    bundle_context = {"accepted_formal_plan_binding": binding.as_dict()}
    plan_context = {"evidence_catalog": catalog}
    with engine.begin() as db:
        db.execute(text("CREATE TABLE rg_target_graphs(graph_ref TEXT, quest_ref TEXT, formal_plan_ref TEXT, plan_content_ref TEXT, plan_document_hash TEXT, context_pack_ref TEXT, context_pack_hash TEXT, request_ref TEXT)"))
        db.execute(text("CREATE TABLE ae_stage_run_requests(request_ref TEXT, context_pack_ref TEXT, context_pack_json TEXT, context_pack_hash TEXT)"))
        db.execute(text("CREATE TABLE rg_formal_plan_decisions(formal_plan_ref TEXT, request_ref TEXT, context_pack_ref TEXT, decision TEXT)"))
        db.execute(text("INSERT INTO rg_target_graphs VALUES ('graph','quest','plan','content',:planhash,'bundle_context',:hash,'bundle_request')"),
            {"planhash": binding.plan_document_hash, "hash": canonical_hash(bundle_context)})
        for prefix, context in (("bundle", bundle_context), ("plan", plan_context)):
            db.execute(text("INSERT INTO ae_stage_run_requests VALUES (:request,:ref,:json,:hash)"),
                {"request": prefix + "_request", "ref": prefix + "_context",
                 "json": canonical_json(context), "hash": canonical_hash(context)})
        db.execute(text("INSERT INTO rg_formal_plan_decisions VALUES ('plan','plan_request','plan_context','accepted')"))
    owner._domain_reader = SimpleNamespace(
        query_target_dependency_asset_bindings=lambda _: ("quest", ()),
        query_target_commit_input_asset_bindings=lambda **kw: {ref: () for ref in kw["target_commit_refs"]},
        query_target_measurement_domain_authority=lambda _: SimpleNamespace(graph_ref="graph",
            target_spec_hash=canonical_hash(spec), accepted_formal_plan_binding_hash=canonical_hash(binding.as_dict())),
        resolve_plan_evidence_reuse_leaves=lambda **kw: leaves,
        query_asset_roles=lambda **kw: tuple(role for role in roles if role.version_ref in kw["version_refs"]),
    )
    return owner


def test_empty_refs_need_no_plan_or_catalog():
    owner = _authority(())
    assert owner.prepare_input_assets("target") is False
    assert owner.input_asset_source_refs("target") == {}


def test_existing_ordinary_asset_proof_needs_no_plan_or_catalog():
    owner = _authority(("asset_direct",), existing=True)
    assert owner.resolve_input_asset_ref(target_ref="target", input_ref="asset_direct") == "asset_direct"
    assert owner.prepare_input_assets("target") is False
    assert owner.input_asset_source_refs("target") == {}


def test_unrelated_selected_plan_version_does_not_block_exact_evidence_ref():
    owner = _authority(("evidence_1",), plan=True)
    assert owner.resolve_input_asset_ref(target_ref="target", input_ref="evidence_1") == "asset_same"
    assert owner.input_asset_source_refs("target") == {"asset_same": ("evidence_1",)}


@pytest.mark.parametrize("refs", [("evidence_1", "evidence_2"), ("asset_same",)])
def test_current_target_ambiguous_asset_versions_are_rejected(refs):
    owner = _authority(refs, plan=True)
    with pytest.raises(OwnerConflict, match="target_input_evidence_version_conflict"):
        owner.resolve_input_asset_ref(target_ref="target", input_ref=refs[0])


def test_unselected_evidence_cannot_resolve():
    owner = _authority(("evidence_unselected",), plan=True)
    with pytest.raises(OwnerConflict, match="target_input_evidence_not_selected"):
        owner.resolve_input_asset_ref(target_ref="target", input_ref="evidence_unselected")


def test_same_asset_existing_version_does_not_override_declared_evidence_version():
    owner = _authority(("asset_same", "evidence_1"), plan=True)
    roles = owner._domain_reader.query_asset_roles(version_refs=("version_2",))
    other = roles[0]
    owner.query_input_asset_projection = lambda **kw: SimpleNamespace(asset=other.asset_binding(),
        source_role_ref=other.role_ref, source_role_receipt=other.receipt)
    with pytest.raises(OwnerConflict, match="target_input_evidence_version_conflict"):
        owner.resolve_input_asset_ref(target_ref="target", input_ref="evidence_1")


def test_declared_duplicate_refs_still_rejected():
    owner = _authority(("asset_direct", "asset_direct"), existing=True)
    with pytest.raises(OwnerConflict, match="target_input_reference_scope_invalid"):
        owner.prepare_input_assets("target")
