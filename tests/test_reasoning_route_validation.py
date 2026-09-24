from types import SimpleNamespace

import pytest

from meta_research.owners.advancement_engine import (
    PRIOR_ACCEPTED_FORMAL_PLAN_SKIP_BASIS_KIND,
    PRIOR_ACCEPTED_IDEA_SET_SKIP_BASIS_KIND,
    _stage_commit_receipt_hash,
    _validate_reasoning_route_rows,
)
from meta_research.owners.common import OwnerConflict


def _stage_row(
    stage: str,
    disposition: str,
    *,
    basis_kind: str | None = None,
    basis_ref: str | None = None,
) -> SimpleNamespace:
    completed = disposition == "completed"
    row = SimpleNamespace(
        commit_ref=f"stage_commit_{stage}",
        request_ref=f"stage_request_{stage}" if completed else None,
        cycle_ref="cycle_successor",
        stage=stage,
        epoch=2,
        run_ref=f"{stage}_run" if completed else None,
        outcome_ref=(
            {"plan": "formal_plan", "bundle": "bundle_report"}.get(stage)
            if completed
            else None
        ),
        outcome_kind=(
            {"plan": "formal_plan", "bundle": "bundle_report"}.get(stage)
            if completed
            else None
        ),
        disposition=disposition,
        run_completion_receipt_ref=f"completion_{stage}" if completed else None,
        run_completion_receipt_hash="a" * 64 if completed else None,
        outcome_receipt_ref=f"outcome_{stage}" if completed else None,
        outcome_receipt_hash="b" * 64 if completed else None,
        closure_hash=None,
        basis_kind=basis_kind,
        basis_ref=basis_ref,
        basis_receipt_issuer="advancement_engine" if basis_ref else None,
        basis_receipt_kind="stage_commit" if basis_ref else None,
        basis_receipt_subject_ref=basis_ref,
        basis_receipt_ref="basis_receipt" if basis_ref else None,
        basis_receipt_hash="c" * 64 if basis_ref else None,
    )
    row.receipt_hash = _stage_commit_receipt_hash(row)
    return row


def _bundle_skip_row() -> SimpleNamespace:
    """Shape written by skip_bundle_stage: request-bound, no basis columns."""

    row = SimpleNamespace(
        commit_ref="stage_commit_bundle",
        request_ref="stage_request_bundle",
        cycle_ref="cycle_successor",
        stage="bundle",
        epoch=2,
        run_ref=None,
        outcome_ref="formal_plan",
        outcome_kind="bundle_skip",
        disposition="skipped",
        run_completion_receipt_ref=None,
        run_completion_receipt_hash=None,
        outcome_receipt_ref="outcome_bundle",
        outcome_receipt_hash="b" * 64,
        closure_hash=None,
        basis_kind=None,
        basis_ref=None,
        basis_receipt_issuer=None,
        basis_receipt_kind=None,
        basis_receipt_subject_ref=None,
        basis_receipt_ref=None,
        basis_receipt_hash=None,
    )
    row.receipt_hash = _stage_commit_receipt_hash(row)
    return row


def test_reused_idea_skip_is_a_valid_reasoning_route() -> None:
    idea = _stage_row(
        "idea",
        "skipped",
        basis_kind=PRIOR_ACCEPTED_IDEA_SET_SKIP_BASIS_KIND,
        basis_ref="stage_commit_prior_idea",
    )
    plan = _stage_row("plan", "completed")
    bundle = _stage_row("bundle", "completed")

    _validate_reasoning_route_rows(
        {"idea": idea, "plan": plan, "bundle": bundle}
    )


def test_successor_bundle_skip_is_a_valid_reasoning_route() -> None:
    """entry=bundle successor whose reused plan carries no experiment gap."""

    idea = _stage_row(
        "idea",
        "skipped",
        basis_kind=PRIOR_ACCEPTED_IDEA_SET_SKIP_BASIS_KIND,
        basis_ref="stage_commit_prior_idea",
    )
    plan = _stage_row(
        "plan",
        "skipped",
        basis_kind=PRIOR_ACCEPTED_FORMAL_PLAN_SKIP_BASIS_KIND,
        basis_ref="stage_commit_prior_plan",
    )
    bundle = _bundle_skip_row()

    _validate_reasoning_route_rows(
        {"idea": idea, "plan": plan, "bundle": bundle}
    )


def test_untyped_all_skipped_bundle_row_stays_invalid() -> None:
    idea = _stage_row(
        "idea",
        "skipped",
        basis_kind=PRIOR_ACCEPTED_IDEA_SET_SKIP_BASIS_KIND,
        basis_ref="stage_commit_prior_idea",
    )
    plan = _stage_row(
        "plan",
        "skipped",
        basis_kind=PRIOR_ACCEPTED_FORMAL_PLAN_SKIP_BASIS_KIND,
        basis_ref="stage_commit_prior_plan",
    )
    bundle = _stage_row("bundle", "skipped")

    with pytest.raises(OwnerConflict, match="reasoning_upstream_closure_invalid"):
        _validate_reasoning_route_rows(
            {"idea": idea, "plan": plan, "bundle": bundle}
        )


def test_untyped_idea_skip_remains_invalid() -> None:
    idea = _stage_row(
        "idea", "skipped", basis_kind="unexpected", basis_ref="prior"
    )
    plan = _stage_row("plan", "completed")
    bundle = _stage_row("bundle", "completed")

    with pytest.raises(OwnerConflict, match="reasoning_upstream_closure_invalid"):
        _validate_reasoning_route_rows(
            {"idea": idea, "plan": plan, "bundle": bundle}
        )
