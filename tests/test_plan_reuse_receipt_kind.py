from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from meta_research.owners.common import AcceptanceReceipt, OwnerConflict
from meta_research.owners.research_graph import TARGET_COMMIT_RECEIPT_KIND
from meta_research.target_commit_evidence import TargetCommitEvidenceCatalog

def receipt(issuer, kind, subject, ref):
    return AcceptanceReceipt(issuer=issuer, kind=kind, subject_ref=subject,
                             receipt_ref=ref, payload_hash='a'*64)

def setup(kind=TARGET_COMMIT_RECEIPT_KIND):
    asset = receipt('research_memory', 'asset', 'asset_version_fixture', 'asset_receipt')
    role = receipt('research_graph', 'role', 'role_fixture', 'role_receipt')
    commit = SimpleNamespace(commit_ref='commit_fixture', receipt=receipt(
        'research_graph', kind, 'commit_fixture', 'commit_receipt'))
    graph = Mock()
    graph.query_target_commits_for_quest.return_value = (commit,)
    catalog = TargetCommitEvidenceCatalog(graph, Mock())
    catalog.verify_plan_evidence_catalog = Mock()
    leaf = object()
    catalog._issuer_closed_role_leaves = Mock(return_value=(leaf,))
    entry = dict(evidence_ref='evidence_fixture', target_commit_root_ref=commit.commit_ref,
                 asset_version_ref=asset.subject_ref, role_ref=role.subject_ref,
                 asset_receipt=asset.as_public_dict(), role_receipt=role.as_public_dict(),
                 integrity_receipt_ref=asset.receipt_ref, availability_receipt_ref=asset.receipt_ref,
                 eligibility_token_ref=role.receipt_ref, currentness_receipt_ref=role.receipt_ref)
    args = dict(quest_ref='quest_fixture', evidence_catalog=[entry],
                expected_reference_revision=1, evidence_reuse_set=[{'evidence_ref':'evidence_fixture'}])
    return catalog, commit, entry, args, leaf

@pytest.mark.parametrize('kind', [TARGET_COMMIT_RECEIPT_KIND, 'target_commit'])
def test_native_and_supported_legacy_receipt_reach_issuer_leaf_validation(kind):
    catalog, commit, entry, args, leaf = setup(kind)
    assert catalog.resolve_plan_evidence_reuse_leaves(**args) == (leaf,)
    catalog.verify_plan_evidence_catalog.assert_called_once()
    catalog._issuer_closed_role_leaves.assert_called_once()

@pytest.mark.parametrize('field,value', [('kind','untrusted'), ('issuer','other'), ('subject_ref','other')])
def test_wrong_commit_receipt_is_rejected(field, value):
    catalog, commit, entry, args, leaf = setup()
    commit.receipt = replace(commit.receipt, **{field:value})
    with pytest.raises(OwnerConflict, match='plan_evidence_reuse_closure_invalid'):
        catalog.resolve_plan_evidence_reuse_leaves(**args)
    catalog._issuer_closed_role_leaves.assert_not_called()

@pytest.mark.parametrize('field', ['integrity_receipt_ref','availability_receipt_ref','eligibility_token_ref','currentness_receipt_ref'])
def test_catalog_proof_mismatch_still_rejected(field):
    catalog, commit, entry, args, leaf = setup()
    entry[field] = 'wrong'
    with pytest.raises(OwnerConflict, match='plan_evidence_reuse_closure_invalid'):
        catalog.resolve_plan_evidence_reuse_leaves(**args)
    catalog._issuer_closed_role_leaves.assert_not_called()
