from __future__ import annotations

from dataclasses import replace

import pytest

from meta_research.bundle_completion import verify_accepted_closure
from meta_research.bundle_protocol import (
    BundleProtocolError,
    ReceiptProof,
    projection_plain_value,
)
from meta_research.owners.common import OwnerConflict
from sqlalchemy import text
from meta_research.target_run_finalizer import TargetRunFinalizer
from test_bundle_completion_contract import _closure, _plan
from test_target_root_finalizer import _EvidenceReader, _root_finalizer_fixture


def _root_receipt(subject_ref: str) -> ReceiptProof:
    return ReceiptProof(
        receipt_ref="ar-target-root-completion-receipt-1",
        subject_ref=subject_ref,
        verified=True,
        currentness_known=True,
        current=True,
    )


def test_root_completion_closure_requires_the_issuer_receipt_instead_of_reviews() -> None:
    _request, plan = _plan({"experiment-a": ("cell-a",)})
    legacy = _closure("target-a", ("experiment-a",), "cell-a")
    root = replace(
        legacy,
        code_review=None,
        result_review=None,
        root_completion_receipt=_root_receipt(legacy.execution_attempt_ref),
    )

    verify_accepted_closure(
        root,
        {brief.experiment_key: brief for brief in plan.briefs},
    )
    assert "root_completion_receipt" not in projection_plain_value(legacy)
    assert projection_plain_value(root)["root_completion_receipt"] is not None

    with pytest.raises(BundleProtocolError, match="root completion receipt"):
        verify_accepted_closure(
            replace(
                root,
                root_completion_receipt=_root_receipt("another-attempt"),
            ),
            {brief.experiment_key: brief for brief in plan.briefs},
        )

    with pytest.raises(BundleProtocolError, match="cannot claim synthetic reviews"):
        verify_accepted_closure(
            replace(root, code_review=legacy.code_review),
            {brief.experiment_key: brief for brief in plan.briefs},
        )


def test_legacy_closure_still_requires_both_independent_reviews() -> None:
    _request, plan = _plan({"experiment-a": ("cell-a",)})
    legacy = _closure("target-a", ("experiment-a",), "cell-a")
    briefs = {brief.experiment_key: brief for brief in plan.briefs}

    verify_accepted_closure(legacy, briefs)
    with pytest.raises(BundleProtocolError, match="reviews"):
        verify_accepted_closure(replace(legacy, result_review=None), briefs)


def test_research_graph_accepts_and_replays_one_issuer_verified_root_commit(
    tmp_path,
) -> None:
    runtime, lifecycle, memory, _authority, handle, _workspace, evidence = (
        _root_finalizer_fixture(tmp_path)
    )
    try:
        seeded = TargetRunFinalizer(
            lifecycle=lifecycle,
            memory=memory,
            workspace_resolver=runtime.target_run_authorities.agent_runtime,
            evidence_reader=_EvidenceReader(evidence),
            measurement_authority=runtime.owners.research_graph,
        ).finalize(handle=handle, evidence=evidence)
        completion = lifecycle.query_completion(handle.target_ref)
        manifest = memory.query(seeded.manifest_ref)
        assert completion is not None and manifest is not None

        graph = runtime.owners.research_graph
        with runtime._database.read() as connection:
            before = (
                connection.exec_driver_sql(
                    "SELECT COUNT(*) FROM rg_target_root_measurements"
                ).scalar_one(),
                connection.exec_driver_sql(
                    "SELECT COUNT(*) FROM rg_target_commits"
                ).scalar_one(),
            )

        with pytest.raises(OwnerConflict, match="issuer_invalid"):
            graph.accept_target_commit_from_root_completion(
                completion=replace(
                    completion,
                    implementation_revision_ref="forged-revision",
                ),
                manifest=manifest,
                result_document=manifest.result_document,
                idempotency_key="accept-root-commit",
            )
        with runtime._database.read() as connection:
            assert (
                connection.exec_driver_sql(
                    "SELECT COUNT(*) FROM rg_target_root_measurements"
                ).scalar_one(),
                connection.exec_driver_sql(
                    "SELECT COUNT(*) FROM rg_target_commits"
                ).scalar_one(),
            ) == before

        accepted = graph.accept_target_commit_from_root_completion(
            completion=completion,
            manifest=manifest,
            result_document=manifest.result_document,
            idempotency_key="accept-root-commit",
        )
        replay = graph.accept_target_commit_from_root_completion(
            completion=completion,
            manifest=manifest,
            result_document=manifest.result_document,
            idempotency_key="accept-root-commit",
        )
        assert replay == accepted
        assert accepted.target_ref == handle.target_ref
        assert accepted.target_run_ref == handle.target_run_ref
        assert accepted.receipt.subject_ref == accepted.target_commit_ref

        transition = graph.query_target_frontier_commit_transition(
            handle.target_ref
        )
        assert transition is not None
        assert transition.target_commit_ref == accepted.target_commit_ref
        assert transition.target_execution_closure_ref == completion.completion_ref
        assert transition.canonical_terminal.code_review is None
        assert transition.canonical_terminal.result_review is None
        assert transition.canonical_terminal.root_completion_receipt is not None
        assert (
            transition.canonical_terminal.root_completion_receipt.subject_ref
            == handle.execution_attempt_ref
        )
        with runtime._database.read() as connection:
            graph_ref = connection.execute(
                text(
                    "SELECT graph_ref FROM rg_targets WHERE target_ref = "
                    ":target_ref"
                ),
                {"target_ref": handle.target_ref},
            ).scalar_one()
            assert connection.exec_driver_sql(
                "SELECT COUNT(*) FROM rg_target_root_measurements"
            ).scalar_one() == 1
            assert connection.exec_driver_sql(
                "SELECT COUNT(*) FROM rg_target_commits"
            ).scalar_one() == 1
        graph.verify_bundle_report_target_commits(
            graph_ref=graph_ref,
            closures=(transition.canonical_terminal,),
            receipts=(accepted.receipt,),
            head_receipt=graph.query_target_graph_head(graph_ref).receipt,
        )

        # The transition reader re-enters RM; a locally self-consistent RG row
        # cannot hide a later-corrupted issuer receipt.
        with runtime._database.write() as connection:
            connection.execute(
                text(
                    "UPDATE rm_target_root_completion_manifests SET "
                    "receipt_hash = :forged WHERE manifest_ref = :manifest_ref"
                ),
                {
                    "forged": "0" * 64,
                    "manifest_ref": manifest.manifest_ref,
                },
            )
        with pytest.raises(OwnerConflict, match="manifest_integrity_invalid"):
            graph.query_target_frontier_commit_transition(handle.target_ref)
    finally:
        runtime.close()
