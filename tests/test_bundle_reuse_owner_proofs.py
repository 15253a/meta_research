from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import text

from meta_research.bundle_protocol import ContentBindingProof, ReceiptProof
from meta_research.bundle_reuse_owner_proofs import (
    BundleTargetCandidateOwnerProofVerifier,
)
from meta_research.composition import build_production_runtime
from meta_research.owners.common import OwnerConflict, canonical_hash, canonical_json
from meta_research.owners.research_graph import (
    TARGET_COMMIT_RECEIPT_KIND,
    _receipt_hash as _rg_receipt_hash,
)
from meta_research.paths import prepare_data_root
from test_public_bundle_stage import (
    _DeterministicBundleSkill,
    _bundle_runtime,
    _confirm_direct_quest,
    _finish_idea_stage,
    _finish_plan_stage,
)
from test_target_launch_admission import _ready_launch


SOURCE_REF = "source:rare-morphology-comparison"
VERSION_REF = "source-version:rare-morphology-comparison"
IMPLEMENTATION_REF = "implementation:rare-morphology-comparison"


def _proof(receipt) -> ReceiptProof:
    return ReceiptProof(
        receipt_ref=receipt.receipt_ref,
        subject_ref=receipt.subject_ref,
        verified=True,
        currentness_known=True,
        current=True,
    )


class _OwnerProofBundleSkill(_DeterministicBundleSkill):
    accepted = None
    forge_source_receipt = False

    def _target_plan(self, request):
        plan = super()._target_plan(request)
        accepted = self.accepted
        assert accepted is not None
        source = plan["initial_strategy_update"]["candidates"][0]["candidate"][
            "reuse_trace"
        ]["tier_decisions"][0]["source_proofs"][0]
        source["verification_receipt"] = {
            "receipt_ref": (
                "forged-source-receipt"
                if self.forge_source_receipt
                else accepted.source_verification_receipt.receipt_ref
            ),
            "subject_ref": accepted.source_verification_receipt.subject_ref,
            "verified": True,
            "currentness_known": True,
            "current": True,
        }
        source["implementation_binding"]["content_hash_ref"] = (
            accepted.content_hash_ref
        )
        source["implementation_acceptance_receipt"] = {
            "receipt_ref": accepted.content_acceptance_receipt.receipt_ref,
            "subject_ref": accepted.content_acceptance_receipt.subject_ref,
            "verified": True,
            "currentness_known": True,
            "current": True,
        }
        return plan


def _accept_implementation(runtime):
    return runtime.owners.research_memory.accept_implementation_content(
        source_ref=SOURCE_REF,
        exact_version_ref=VERSION_REF,
        implementation_revision_ref=IMPLEMENTATION_REF,
        verification_evidence_ref="source-pin-verification:evidence",
        idempotency_key="accept-fixed-implementation",
    )


def _install_accepted_target_commit(runtime, *, graph, target) -> str:
    """Install an issuer-valid commit row around the real accepted Target.

    TargetCommit measurement admission is independently covered by its public
    suite.  This fixture keeps this test focused on the new eligibility seam
    while still making RG re-read and validate the canonical Target, graph and
    TargetCommit receipt rather than trusting a caller-provided anchor label.
    """

    commit_ref = "target_commit_owner_reuse_anchor"
    target_run_ref = "target_run_owner_reuse_anchor"
    evaluation_attempt_ref = "evaluation_attempt_owner_reuse_anchor"
    closure = {"schema_ref": "fixture/accepted-target-commit-closure/v1"}
    closure_hash = canonical_hash(closure)
    bindings = {
        "target_ref": target.target_ref,
        "target_run_ref": target_run_ref,
        "evaluation_attempt_ref": evaluation_attempt_ref,
        "target_spec_hash": target.spec_hash,
        "closure_hash": closure_hash,
        "result_disposition": "positive",
    }
    values = {
        **bindings,
        "commit_ref": commit_ref,
        "closure_json": canonical_json(closure),
        "receipt_ref": "rg_target_commit_receipt_owner_reuse_anchor",
        "receipt_hash": _rg_receipt_hash(
            TARGET_COMMIT_RECEIPT_KIND,
            commit_ref,
            bindings,
        ),
    }
    # The focused fixture does not synthesize an Experiment run merely to
    # satisfy TargetCommit's two execution FKs.  FK checks are disabled only
    # for this direct fixture connection; the row itself is still read and
    # cryptographically/canonically verified by the production RG query.
    database_path = str(runtime._database._engine.url.database)
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute(
            "INSERT INTO rg_target_commits (commit_ref, target_ref, "
            "target_run_ref, evaluation_attempt_ref, target_spec_hash, "
            "closure_json, closure_hash, result_disposition, receipt_ref, "
            "receipt_hash, committed_at) VALUES (:commit_ref, :target_ref, "
            ":target_run_ref, :evaluation_attempt_ref, :target_spec_hash, "
            ":closure_json, :closure_hash, :result_disposition, :receipt_ref, "
            ":receipt_hash, 1.0)",
            values,
        )
        connection.execute(
            "UPDATE research_graph_state SET target_commit_count = "
            "target_commit_count + 1, revision = revision + 1 WHERE "
            "singleton = 'owner'"
        )
    commits = runtime.owners.research_graph.query_target_commits(graph.graph_ref)
    assert len(commits) == 1 and commits[0].commit_ref == commit_ref
    return commit_ref


def test_rm_content_receipts_keep_exact_subjects_and_survive_restart(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "reuse-rm-restart"
    runtime = build_production_runtime(prepare_data_root(data_root))
    try:
        accepted = _accept_implementation(runtime)
        expected_content = {
            "source_ref": SOURCE_REF,
            "exact_version_ref": VERSION_REF,
            "implementation_revision_ref": IMPLEMENTATION_REF,
            "license_ref": None,
            "source_content_hash_ref": None,
            "patch_ref": None,
        }
        assert accepted.content == expected_content
        assert accepted.content_hash_ref == canonical_hash(expected_content)
        assert accepted.source_verification_receipt.subject_ref == VERSION_REF
        assert (
            accepted.content_acceptance_receipt.subject_ref
            == accepted.content_hash_ref
        )
        external_hash = canonical_hash({"external-source": "exact bytes"})
        external = runtime.owners.research_memory.accept_implementation_content(
            source_ref="source:external",
            exact_version_ref="source-version:external-v1",
            implementation_revision_ref="implementation:external-v1",
            verification_evidence_ref="source-pin-verification:external-v1",
            license_ref="license:apache-2.0",
            source_content_hash_ref=external_hash,
            patch_ref="patch:none",
            idempotency_key="accept-external-implementation",
        )
        verifier = BundleTargetCandidateOwnerProofVerifier(
            runtime.owners.research_memory,
            runtime.owners.research_graph,
        )
        verifier.verify_reuse_source_receipt(
            tier="mature-external",
            source_ref=external.source_ref,
            exact_version_ref=external.exact_version_ref,
            implementation_revision_ref=external.implementation_revision_ref,
            license_ref=external.license_ref,
            source_content_hash_ref=external.source_content_hash_ref,
            patch_ref=external.patch_ref,
            receipt=_proof(external.source_verification_receipt),
        )
        with pytest.raises(
            OwnerConflict, match="mature_external_source_proof_incomplete"
        ):
            verifier.verify_reuse_source_receipt(
                tier="mature-external",
                source_ref=accepted.source_ref,
                exact_version_ref=accepted.exact_version_ref,
                implementation_revision_ref=accepted.implementation_revision_ref,
                license_ref=None,
                source_content_hash_ref=None,
                patch_ref=None,
                receipt=_proof(accepted.source_verification_receipt),
            )
    finally:
        runtime.close()

    restarted = build_production_runtime(prepare_data_root(data_root))
    try:
        accepted = restarted.owners.research_memory.query_implementation_content(
            IMPLEMENTATION_REF
        )
        assert accepted is not None
        verifier = BundleTargetCandidateOwnerProofVerifier(
            restarted.owners.research_memory,
            restarted.owners.research_graph,
        )
        verifier.verify_reuse_source_receipt(
            tier="self-implementation",
            source_ref=SOURCE_REF,
            exact_version_ref=VERSION_REF,
            implementation_revision_ref=IMPLEMENTATION_REF,
            license_ref=None,
            source_content_hash_ref=None,
            patch_ref=None,
            receipt=_proof(accepted.source_verification_receipt),
        )
        with pytest.raises(OwnerConflict, match="implementation_content_receipt"):
            verifier.verify_reuse_content_receipt(
                tier="self-implementation",
                source_ref=SOURCE_REF,
                exact_version_ref=VERSION_REF,
                implementation_revision_ref=IMPLEMENTATION_REF,
                license_ref=None,
                source_content_hash_ref=None,
                patch_ref=None,
                binding=ContentBindingProof(
                    IMPLEMENTATION_REF, accepted.content_hash_ref
                ),
                receipt=ReceiptProof(
                    accepted.content_acceptance_receipt.receipt_ref,
                    IMPLEMENTATION_REF,
                    True,
                    True,
                    True,
                ),
            )
        with restarted._database.write() as connection:
            connection.execute(
                text(
                    "UPDATE rm_implementation_revision_contents SET patch_ref = "
                    "'tampered-patch' WHERE implementation_revision_ref = "
                    ":implementation_revision_ref"
                ),
                {"implementation_revision_ref": IMPLEMENTATION_REF},
            )
        with pytest.raises(OwnerConflict, match="implementation_content_invalid"):
            restarted.owners.research_memory.query_implementation_content(
                IMPLEMENTATION_REF
            )
    finally:
        restarted.close()


def test_owner_verifier_rejects_before_rg_creates_any_target_row(
    tmp_path: Path,
) -> None:
    skill = _OwnerProofBundleSkill()
    skill.forge_source_receipt = True
    runtime = _bundle_runtime(
        tmp_path / "reuse-proof-before-target",
        bundle_skill_provider=skill,
        verify_target_candidate_proofs=False,
    )
    try:
        skill.accepted = _accept_implementation(runtime)
        runtime.owners.research_graph.bind_target_candidate_proof_verifier(
            BundleTargetCandidateOwnerProofVerifier(
                runtime.owners.research_memory,
                runtime.owners.research_graph,
            )
        )
        _confirm_direct_quest(runtime)
        _finish_idea_stage(runtime)
        _finish_plan_stage(runtime)
        rejection = None
        for _step in range(8):
            assert runtime.bundle_stage.process_once()
            current = runtime.bundle_stage.query_current()
            run = current.get("run")
            submission_ref = None if run is None else run.get("submission_ref")
            if not isinstance(submission_ref, str):
                continue
            rejection = runtime.owners.research_graph.query_target_graph_rejection(
                submission_ref
            )
            if rejection is not None:
                break
        else:
            raise AssertionError("forged source receipt reached RG Target creation")
        assert rejection is not None
        assert rejection.reason_code == "target_candidate_owner_proof_unverified"
        with runtime._database.read() as connection:
            assert connection.execute(
                text("SELECT COUNT(*) FROM rg_targets")
            ).scalar_one() == 0
    finally:
        runtime.close()


def test_rg_eligibility_is_content_bound_to_current_accepted_target_commit(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "reuse-eligibility"
    skill = _OwnerProofBundleSkill()
    runtime = _bundle_runtime(
        data_root,
        bundle_skill_provider=skill,
        verify_target_candidate_proofs=False,
    )
    try:
        accepted = _accept_implementation(runtime)
        skill.accepted = accepted
        runtime.owners.research_graph.bind_target_candidate_proof_verifier(
            BundleTargetCandidateOwnerProofVerifier(
                runtime.owners.research_memory,
                runtime.owners.research_graph,
            )
        )
        graph, target, _run, _dispatch, _request = _ready_launch(runtime)
        with pytest.raises(OwnerConflict, match="reuse_eligibility_anchor_invalid"):
            runtime.owners.research_graph.accept_reuse_eligibility(
                tier="accepted-local",
                target_commit_ref="target_commit_missing",
                source_ref=SOURCE_REF,
                exact_version_ref=VERSION_REF,
                implementation_revision_ref=IMPLEMENTATION_REF,
                implementation_content_hash_ref=accepted.content_hash_ref,
                idempotency_key="missing-anchor",
            )
        commit_ref = _install_accepted_target_commit(
            runtime, graph=graph, target=target
        )
        eligibility = runtime.owners.research_graph.accept_reuse_eligibility(
            tier="accepted-local",
            target_commit_ref=commit_ref,
            source_ref=SOURCE_REF,
            exact_version_ref=VERSION_REF,
            implementation_revision_ref=IMPLEMENTATION_REF,
            implementation_content_hash_ref=accepted.content_hash_ref,
            idempotency_key="accepted-local-eligibility",
        )
        assert eligibility.receipt.subject_ref == eligibility.payload_hash
        assert eligibility.content_binding().content_hash_ref == eligibility.payload_hash
        verifier = BundleTargetCandidateOwnerProofVerifier(
            runtime.owners.research_memory,
            runtime.owners.research_graph,
        )
        verifier.verify_reuse_eligibility_receipt(
            tier="accepted-local",
            source_ref=SOURCE_REF,
            exact_version_ref=VERSION_REF,
            implementation_revision_ref=IMPLEMENTATION_REF,
            implementation_content_hash_ref=accepted.content_hash_ref,
            eligibility_anchor_ref=commit_ref,
            binding=eligibility.content_binding(),
            receipt=_proof(eligibility.receipt),
        )
        with pytest.raises(OwnerConflict, match="reuse_eligibility_receipt_invalid"):
            verifier.verify_reuse_eligibility_receipt(
                tier="related-history",
                source_ref=SOURCE_REF,
                exact_version_ref=VERSION_REF,
                implementation_revision_ref=IMPLEMENTATION_REF,
                implementation_content_hash_ref=accepted.content_hash_ref,
                eligibility_anchor_ref=commit_ref,
                binding=eligibility.content_binding(),
                receipt=_proof(eligibility.receipt),
            )
        eligibility_ref = eligibility.eligibility_ref
    finally:
        runtime.close()

    restarted = build_production_runtime(prepare_data_root(data_root))
    try:
        assert restarted.owners.research_graph.query_reuse_eligibility(
            eligibility_ref
        ) is not None
        with restarted._database.write() as connection:
            connection.execute(
                text(
                    "UPDATE rg_reuse_eligibilities SET source_ref = "
                    "'source:tampered' WHERE eligibility_ref = :eligibility_ref"
                ),
                {"eligibility_ref": eligibility_ref},
            )
        with pytest.raises(OwnerConflict, match="reuse_eligibility_invalid"):
            restarted.owners.research_graph.query_reuse_eligibility(
                eligibility_ref
            )
    finally:
        restarted.close()
