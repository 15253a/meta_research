#!/usr/bin/env python3
"""Contract tests for the deterministic Idea Stage fixture."""

from __future__ import annotations

import unittest
from dataclasses import asdict, replace

from idea_stage_mvp import (
    AdvisoryReviewRecord,
    CallLedger,
    ContractViolation,
    ExhaustionClosure,
    FakeAdvancementPort,
    FakeContentPort,
    FakeDomainPort,
    FakeInvocationPort,
    FakeRuntimePort,
    FixtureRecoveryDisposition,
    FixtureRunObservation,
    FixtureOwnerReply,
    OwnerFeedbackRevisionRecord,
    ReviewDisposition,
    ReviewedOutcome,
    SubmissionIdentity,
    SubmissionIdentityRegistry,
    SubmissionResult,
    build_exhaustion_proposal,
    canonical_hash,
    fixture_hash,
    fixture_idea_set,
    fixture_no_viable,
    fixture_request_and_pack,
    fixture_review,
    make_context_pack,
    reconcile_exhaustion_proposal,
    submit_exhaustion_proposal,
    submit_reviewed_outcome,
    verify_invocation,
    verify_same_feedback_loop,
)


FORBIDDEN_OPERATIONS = {
    "ResearchGraph.create_question",
    "ResearchGraph.modify_question",
    "DeepFetch.start",
    "AgentRuntime.accept_run",
    "AdvancementEngine.issue_stage_run_request",
    "AdvancementEngine.form_stage_commit",
}


def accepted_content_reply() -> FixtureOwnerReply:
    return FixtureOwnerReply(
        status="accepted",
        receipt_ref="fixture:rm/receipt/content-test",
        accepted_ref="fixture:rm/asset/idea-content-test",
    )


def accepted_domain_reply() -> FixtureOwnerReply:
    return FixtureOwnerReply(
        status="accepted",
        receipt_ref="fixture:rg/receipt/outcome-test",
        accepted_ref="fixture:rg/idea-outcome/test",
    )


class IdeaStageMvpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.request, self.pack = fixture_request_and_pack()

    def clean_exhaustion_closure(self) -> ExhaustionClosure:
        return ExhaustionClosure(
            exploration_record_refs=("fixture:agent/exploration/clean",),
            prior_submission_refs=(),
            owner_rejection_receipt_refs=(),
            cannot_form_idea_set_reason="No materially distinct candidate remains.",
            cannot_form_no_viable_reason=(
                "The evidence cannot support a bounded negative outcome."
            ),
        )

    def submit(
        self,
        outcome,
        domain_reply=None,
        content_reply=None,
        content_failure=None,
        domain_failure=None,
        content_reconcile_reply=None,
        domain_reconcile_reply=None,
        content_reconcile_failure=None,
        domain_reconcile_failure=None,
        submission=None,
        registry=None,
        observed_requests=(),
        reviewed=None,
        runtime_observation=None,
        runtime_reported_blocker_refs=(),
    ):
        ledger = CallLedger()
        invocation = FakeInvocationPort(
            ledger,
            self.request,
            self.pack,
            observed_requests=observed_requests,
        )
        result = submit_reviewed_outcome(
            self.request.ref,
            self.pack.ref,
            self.pack.content_sha256,
            submission or SubmissionIdentity("fixture:agent/submission/test-1"),
            reviewed or fixture_review(outcome),
            invocation,
            FakeContentPort(
                ledger,
                content_reply or accepted_content_reply(),
                technical_failure=content_failure,
                reconcile_reply=content_reconcile_reply,
                reconcile_technical_failure=content_reconcile_failure,
            ),
            FakeDomainPort(
                ledger,
                domain_reply or accepted_domain_reply(),
                technical_failure=domain_failure,
                reconcile_reply=domain_reconcile_reply,
                reconcile_technical_failure=domain_reconcile_failure,
            ),
            FakeRuntimePort(
                ledger,
                runtime_observation,
                reported_blocker_refs=runtime_reported_blocker_refs,
            ),
            registry or SubmissionIdentityRegistry(),
        )
        return result, ledger

    def assert_no_forbidden_authority(self, ledger: CallLedger) -> None:
        self.assertTrue(FORBIDDEN_OPERATIONS.isdisjoint(ledger.events))

    def test_reviewed_idea_set_accepts_one_and_four_candidates_without_top_k(self):
        for count in (1, 4):
            with self.subTest(candidate_count=count):
                result, ledger = self.submit(fixture_idea_set(count))
                self.assertEqual("accepted", result.status)
                self.assertEqual(
                    "fixture:rm/asset/idea-content-test", result.content_ref
                )
                self.assertEqual(
                    "fixture:rg/idea-outcome/test", result.outcome_ref
                )
                self.assertTrue(result.simulated_domain_accepted)
                self.assertFalse(result.is_owner_fact)
                self.assertFalse(result.is_stage_advanced)
                self.assertFalse(result.stage_commit_created_by_skill)
                self.assertEqual(
                    [
                        "AdvancementEngine.observe_idea_stage_run",
                        "ProjectionStore.read_frozen_context_pack",
                        "AgentRuntime.observe_run",
                        "AdvancementEngine.observe_idea_stage_run",
                        "ResearchMemory.accept_idea_outcome_content",
                        "AgentRuntime.observe_run",
                        "AdvancementEngine.observe_idea_stage_run",
                        "ResearchGraph.submit_idea_outcome",
                    ],
                    ledger.events,
                )
                self.assert_no_forbidden_authority(ledger)

    def test_reviewed_no_viable_is_accepted_outcome_not_exhaustion(self):
        result, ledger = self.submit(fixture_no_viable())
        self.assertEqual("accepted", result.status)
        self.assertTrue(result.simulated_domain_accepted)
        self.assertFalse(result.is_owner_fact)
        self.assertNotIn(
            "AdvancementEngine.submit_exhaustion_proposal", ledger.events
        )
        self.assertFalse(result.is_stage_advanced)
        self.assert_no_forbidden_authority(ledger)

    def test_outcome_shape_has_no_winner_score_or_stage_authority(self):
        payload = asdict(fixture_idea_set(4))
        serialized_keys = repr(payload)
        for forbidden in (
            "selected_id",
            "audit_scores",
            "winner",
            "owner_accepted",
            "stage_commit",
        ):
            self.assertNotIn(forbidden, serialized_keys)

    def test_free_text_and_legacy_mapping_fail_before_any_port_call(self):
        for request_ref, pack_ref, bad_outcome in (
            ("please brainstorm", self.pack.ref, fixture_idea_set()),
            (self.request.ref, "fixture:projection/context-pack/latest", fixture_idea_set()),
            (
                self.request.ref,
                self.pack.ref,
                {"candidates": [], "selected_id": None, "audit_scores": []},
            ),
        ):
            with self.subTest(request_ref=request_ref, pack_ref=pack_ref):
                ledger = CallLedger()
                with self.assertRaises(ContractViolation):
                    submit_reviewed_outcome(
                        request_ref,
                        pack_ref,
                        self.pack.content_sha256,
                        SubmissionIdentity("fixture:agent/submission/bad"),
                        (
                            fixture_review(bad_outcome)
                            if not isinstance(bad_outcome, dict)
                            else bad_outcome
                        ),
                        FakeInvocationPort(ledger, self.request, self.pack),
                        FakeContentPort(ledger, accepted_content_reply()),
                        FakeDomainPort(ledger, accepted_domain_reply()),
                        FakeRuntimePort(ledger),
                        SubmissionIdentityRegistry(),
                    )
                self.assertEqual([], ledger.events)

    def test_wrong_stage_untyped_or_stale_request_fails_closed(self):
        bad_requests = (
            replace(self.request, stage="Plan"),
            replace(self.request, contract_id="unknown"),
            replace(self.request, is_current=False),
            replace(self.request, root_fence_current=False),
            replace(self.request, explicit_invocation=False),
        )
        for bad_request in bad_requests:
            with self.subTest(request=bad_request):
                ledger = CallLedger()
                with self.assertRaises(ContractViolation):
                    submit_reviewed_outcome(
                        bad_request.ref,
                        self.pack.ref,
                        self.pack.content_sha256,
                        SubmissionIdentity("fixture:agent/submission/bad-request"),
                        fixture_review(fixture_idea_set()),
                        FakeInvocationPort(ledger, bad_request, self.pack),
                        FakeContentPort(ledger, accepted_content_reply()),
                        FakeDomainPort(ledger, accepted_domain_reply()),
                        FakeRuntimePort(ledger),
                        SubmissionIdentityRegistry(),
                    )
                self.assertEqual(
                    ["AdvancementEngine.observe_idea_stage_run"], ledger.events
                )

    def test_mutable_or_drifted_context_pack_fails_closed(self):
        drifted_pack = replace(self.pack, content_sha256=fixture_hash("drifted"))
        bad_packs = (
            drifted_pack,
            replace(self.pack, bound_stage_run_request_ref="fixture:ae/other-request"),
        )
        for bad_pack in bad_packs:
            with self.subTest(pack=type(bad_pack).__name__):
                ledger = CallLedger()
                with self.assertRaises(ContractViolation):
                    submit_reviewed_outcome(
                        self.request.ref,
                        self.pack.ref,
                        self.pack.content_sha256,
                        SubmissionIdentity("fixture:agent/submission/bad-pack"),
                        fixture_review(fixture_idea_set()),
                        FakeInvocationPort(ledger, self.request, bad_pack),
                        FakeContentPort(ledger, accepted_content_reply()),
                        FakeDomainPort(ledger, accepted_domain_reply()),
                        FakeRuntimePort(ledger),
                        SubmissionIdentityRegistry(),
                    )
                self.assertEqual(
                    [
                        "AdvancementEngine.observe_idea_stage_run",
                        "ProjectionStore.read_frozen_context_pack",
                    ],
                    ledger.events,
                )

    def test_independent_review_digest_and_dispositions_are_enforced(self):
        outcome = fixture_idea_set()
        valid = fixture_review(outcome)
        bad_reviews = (
            replace(
                valid.review,
                reviewer_session_ref=self.request.root_session_ref,
            ),
            replace(valid.review, reviewed_draft_hash=fixture_hash("wrong-draft")),
            replace(valid.review, final_outcome_hash=fixture_hash("wrong-final")),
            replace(valid.review, dispositions=()),
            replace(valid.review, advisory_only=False),
        )
        for bad_review in bad_reviews:
            with self.subTest(review=bad_review):
                ledger = CallLedger()
                reviewed = ReviewedOutcome(outcome, outcome, bad_review)
                with self.assertRaises(ContractViolation):
                    submit_reviewed_outcome(
                        self.request.ref,
                        self.pack.ref,
                        self.pack.content_sha256,
                        SubmissionIdentity("fixture:agent/submission/bad-review"),
                        reviewed,
                        FakeInvocationPort(ledger, self.request, self.pack),
                        FakeContentPort(ledger, accepted_content_reply()),
                        FakeDomainPort(ledger, accepted_domain_reply()),
                        FakeRuntimePort(ledger),
                        SubmissionIdentityRegistry(),
                    )
                self.assertEqual(
                    [
                        "AdvancementEngine.observe_idea_stage_run",
                        "ProjectionStore.read_frozen_context_pack",
                    ],
                    ledger.events,
                )

    def test_advisory_mapping_cannot_impersonate_review_or_owner_fact(self):
        outcome = fixture_idea_set()
        ledger = CallLedger()
        fake_review = {
            "approved": True,
            "winner": "c1",
            "owner_receipt": "fixture:rg/receipt/fake",
        }
        reviewed = ReviewedOutcome(outcome, outcome, fake_review)  # type: ignore[arg-type]
        with self.assertRaises(ContractViolation):
            submit_reviewed_outcome(
                self.request.ref,
                self.pack.ref,
                self.pack.content_sha256,
                SubmissionIdentity("fixture:agent/submission/reviewer-overreach"),
                reviewed,
                FakeInvocationPort(ledger, self.request, self.pack),
                FakeContentPort(ledger, accepted_content_reply()),
                FakeDomainPort(ledger, accepted_domain_reply()),
                FakeRuntimePort(ledger),
                SubmissionIdentityRegistry(),
            )
        self.assertEqual([], ledger.events)

        authoritative_reply = replace(accepted_content_reply(), is_owner_fact=True)
        with self.assertRaises(ContractViolation):
            submit_reviewed_outcome(
                self.request.ref,
                self.pack.ref,
                self.pack.content_sha256,
                SubmissionIdentity("fixture:agent/submission/fake-authority"),
                fixture_review(outcome),
                FakeInvocationPort(ledger, self.request, self.pack),
                FakeContentPort(ledger, authoritative_reply),
                FakeDomainPort(ledger, accepted_domain_reply()),
                FakeRuntimePort(ledger),
                SubmissionIdentityRegistry(),
            )
        self.assertEqual(
            [
                "AdvancementEngine.observe_idea_stage_run",
                "ProjectionStore.read_frozen_context_pack",
                "AgentRuntime.observe_run",
                "AdvancementEngine.observe_idea_stage_run",
                "ResearchMemory.accept_idea_outcome_content",
            ],
            ledger.events,
        )

    def test_owner_rejection_stays_in_same_feedback_loop_without_exhaustion(self):
        registry = SubmissionIdentityRegistry()
        first_identity = SubmissionIdentity("fixture:agent/submission/test-1")
        initial_outcome = fixture_idea_set()
        initial_reviewed = fixture_review(initial_outcome)
        rejected = FixtureOwnerReply(
            status="rejected",
            receipt_ref="fixture:rg/receipt/rejected-1",
            feedback=("Separate inference from accepted evidence.",),
        )
        first, first_ledger = self.submit(
            initial_outcome,
            domain_reply=rejected,
            submission=first_identity,
            registry=registry,
            reviewed=initial_reviewed,
        )
        self.assertEqual("rejected", first.status)
        self.assertFalse(first.simulated_domain_accepted)
        self.assertEqual(
            "fixture:rg/receipt/rejected-1",
            first.domain_decision_receipt_ref,
        )
        self.assertNotIn(
            "AdvancementEngine.submit_exhaustion_proposal", first_ledger.events
        )

        previous = verify_invocation(self.request, self.pack)
        current = verify_invocation(self.request, self.pack)
        verify_same_feedback_loop(previous, current)

        final_outcome = fixture_idea_set(4)
        revision = OwnerFeedbackRevisionRecord(
            revision_ref="fixture:agent/revision/same-loop",
            prior_review_ref=initial_reviewed.review.review_ref,
            predecessor_submission_ref=first_identity.ref,
            owner_rejection_receipt_ref=first.domain_decision_receipt_ref,
            final_outcome_hash=canonical_hash(final_outcome),
            root_revision_rationale="The root Agent revised the rejected outcome.",
        )
        second, second_ledger = self.submit(
            final_outcome,
            submission=SubmissionIdentity(
                "fixture:agent/submission/test-2",
                predecessor_submission_ref=first_identity.ref,
                owner_rejection_receipt_ref=first.domain_decision_receipt_ref,
            ),
            registry=registry,
            reviewed=ReviewedOutcome(
                initial_outcome,
                final_outcome,
                revision,
            ),
        )
        self.assertEqual("accepted", second.status)
        self.assertNotIn(
            "AdvancementEngine.submit_exhaustion_proposal", second_ledger.events
        )

        changed_session = replace(
            self.request, root_session_ref="fixture:ar/session/new-root"
        )
        changed = verify_invocation(changed_session, self.pack)
        with self.assertRaises(ContractViolation):
            verify_same_feedback_loop(previous, changed)

    def test_submission_identity_allows_exact_replay_but_rejects_new_payload(self):
        registry = SubmissionIdentityRegistry()
        identity = SubmissionIdentity("fixture:agent/submission/stable")
        first, _ = self.submit(
            fixture_idea_set(), submission=identity, registry=registry
        )
        replay, replay_ledger = self.submit(
            fixture_idea_set(), submission=identity, registry=registry
        )
        self.assertEqual("accepted", first.status)
        self.assertEqual("accepted", replay.status)
        self.assertNotIn(
            "ResearchMemory.accept_idea_outcome_content", replay_ledger.events
        )
        self.assertNotIn("ResearchGraph.submit_idea_outcome", replay_ledger.events)

        ledger = CallLedger()
        with self.assertRaises(ContractViolation):
            submit_reviewed_outcome(
                self.request.ref,
                self.pack.ref,
                self.pack.content_sha256,
                identity,
                fixture_review(fixture_idea_set(4)),
                FakeInvocationPort(ledger, self.request, self.pack),
                FakeContentPort(ledger, accepted_content_reply()),
                FakeDomainPort(ledger, accepted_domain_reply()),
                FakeRuntimePort(ledger),
                registry,
            )
        self.assertNotIn("ResearchMemory.accept_idea_outcome_content", ledger.events)

    def test_owner_rejection_revision_does_not_force_a_second_advisory_review(self):
        registry = SubmissionIdentityRegistry()
        draft = fixture_idea_set()
        initial_reviewed = fixture_review(draft)
        first_identity = SubmissionIdentity("fixture:agent/submission/reviewed-first")
        rejected, _ = self.submit(
            draft,
            submission=first_identity,
            registry=registry,
            reviewed=initial_reviewed,
            domain_reply=FixtureOwnerReply(
                status="rejected",
                receipt_ref="fixture:rg/receipt/root-revision-needed",
                feedback=("Clarify the candidate separation.",),
            ),
        )

        final = fixture_idea_set(4)
        fresh_advisory = fixture_review(final)
        fresh_advisory = replace(
            fresh_advisory,
            review=replace(
                fresh_advisory.review,
                review_ref="fixture:review/unlinked-after-rejection",
            ),
        )
        with self.assertRaises(ContractViolation):
            self.submit(
                final,
                submission=SubmissionIdentity(
                    "fixture:agent/submission/fresh-review-bypass",
                    predecessor_submission_ref=first_identity.ref,
                    owner_rejection_receipt_ref=(
                        rejected.domain_decision_receipt_ref
                    ),
                ),
                registry=registry,
                reviewed=fresh_advisory,
            )

        unchanged_identity = SubmissionIdentity(
            "fixture:agent/submission/metadata-only-second",
            predecessor_submission_ref=first_identity.ref,
            owner_rejection_receipt_ref=rejected.domain_decision_receipt_ref,
        )
        metadata_only = OwnerFeedbackRevisionRecord(
            revision_ref="fixture:agent/revision/metadata-only",
            prior_review_ref=initial_reviewed.review.review_ref,
            predecessor_submission_ref=first_identity.ref,
            owner_rejection_receipt_ref=rejected.domain_decision_receipt_ref,
            final_outcome_hash=canonical_hash(draft),
            root_revision_rationale="Only revision metadata changed.",
        )
        with self.assertRaises(ContractViolation):
            self.submit(
                draft,
                submission=unchanged_identity,
                registry=registry,
                reviewed=ReviewedOutcome(draft, draft, metadata_only),
            )

        wrong_review_identity = SubmissionIdentity(
            "fixture:agent/submission/wrong-review-lineage",
            predecessor_submission_ref=first_identity.ref,
            owner_rejection_receipt_ref=rejected.domain_decision_receipt_ref,
        )
        wrong_review_lineage = OwnerFeedbackRevisionRecord(
            revision_ref="fixture:agent/revision/wrong-review",
            prior_review_ref="fixture:review/completely-wrong",
            predecessor_submission_ref=first_identity.ref,
            owner_rejection_receipt_ref=rejected.domain_decision_receipt_ref,
            final_outcome_hash=canonical_hash(final),
            root_revision_rationale="Outcome changed but review lineage is wrong.",
        )
        with self.assertRaises(ContractViolation):
            self.submit(
                final,
                submission=wrong_review_identity,
                registry=registry,
                reviewed=ReviewedOutcome(draft, final, wrong_review_lineage),
            )

        second_identity = SubmissionIdentity(
            "fixture:agent/submission/root-revised-second",
            predecessor_submission_ref=first_identity.ref,
            owner_rejection_receipt_ref=rejected.domain_decision_receipt_ref,
        )
        revision = OwnerFeedbackRevisionRecord(
            revision_ref="fixture:agent/revision/root-1",
            prior_review_ref=initial_reviewed.review.review_ref,
            predecessor_submission_ref=first_identity.ref,
            owner_rejection_receipt_ref=rejected.domain_decision_receipt_ref,
            final_outcome_hash=canonical_hash(final),
            root_revision_rationale=(
                "The root Agent clarified separation in response to Owner feedback."
            ),
        )
        revised = ReviewedOutcome(draft, final, revision)
        accepted, _ = self.submit(
            final,
            submission=second_identity,
            registry=registry,
            reviewed=revised,
        )
        self.assertEqual("accepted", accepted.status)

    def test_successor_requires_changed_payload_and_exact_rejection_link(self):
        registry = SubmissionIdentityRegistry()
        predecessor = SubmissionIdentity("fixture:agent/submission/predecessor")
        rejected, _ = self.submit(
            fixture_idea_set(),
            submission=predecessor,
            registry=registry,
            domain_reply=FixtureOwnerReply(
                status="rejected",
                receipt_ref="fixture:rg/receipt/exact-rejection",
                feedback=("Revise the evidence boundary.",),
            ),
        )
        self.assertEqual("rejected", rejected.status)

        with self.assertRaises(ContractViolation):
            self.submit(
                fixture_idea_set(4),
                submission=SubmissionIdentity(
                    "fixture:agent/submission/orphan-revision"
                ),
                registry=registry,
            )
        with self.assertRaises(ContractViolation):
            self.submit(
                fixture_idea_set(4),
                submission=SubmissionIdentity(
                    "fixture:agent/submission/wrong-link",
                    predecessor_submission_ref=predecessor.ref,
                    owner_rejection_receipt_ref="fixture:rg/receipt/wrong",
                ),
                registry=registry,
            )
        with self.assertRaises(ContractViolation):
            self.submit(
                fixture_idea_set(),
                submission=SubmissionIdentity(
                    "fixture:agent/submission/unchanged-successor",
                    predecessor_submission_ref=predecessor.ref,
                    owner_rejection_receipt_ref=(
                        rejected.domain_decision_receipt_ref
                    ),
                ),
                registry=registry,
            )

        changed_root = replace(
            self.request, root_session_ref="fixture:ar/session/other-root"
        )
        ledger = CallLedger()
        with self.assertRaises(ContractViolation):
            submit_reviewed_outcome(
                changed_root.ref,
                self.pack.ref,
                self.pack.content_sha256,
                SubmissionIdentity(
                    "fixture:agent/submission/cross-root-successor",
                    predecessor_submission_ref=predecessor.ref,
                    owner_rejection_receipt_ref=(
                        rejected.domain_decision_receipt_ref
                    ),
                ),
                fixture_review(fixture_idea_set(4)),
                FakeInvocationPort(ledger, changed_root, self.pack),
                FakeContentPort(ledger, accepted_content_reply()),
                FakeDomainPort(ledger, accepted_domain_reply()),
                FakeRuntimePort(ledger),
                registry,
            )
        self.assertNotIn("ResearchMemory.accept_idea_outcome_content", ledger.events)

    def test_nonaccepted_content_and_domain_receipts_are_preserved(self):
        content_rejected, content_ledger = self.submit(
            fixture_idea_set(),
            content_reply=FixtureOwnerReply(
                status="rejected",
                receipt_ref="fixture:rm/receipt/content-rejection",
                feedback=("Content provenance is incomplete.",),
            ),
        )
        self.assertEqual("content", content_rejected.decision_phase)
        self.assertEqual(
            "fixture:rm/receipt/content-rejection",
            content_rejected.content_decision_receipt_ref,
        )
        self.assertNotIn("ResearchGraph.submit_idea_outcome", content_ledger.events)

        domain_rejected, _ = self.submit(
            fixture_idea_set(),
            domain_reply=FixtureOwnerReply(
                status="rejected",
                receipt_ref="fixture:rg/receipt/domain-rejection",
                feedback=("The outcome is not accepted.",),
            ),
        )
        self.assertEqual(
            "fixture:rm/receipt/content-test",
            domain_rejected.content_decision_receipt_ref,
        )
        self.assertEqual(
            "fixture:rg/receipt/domain-rejection",
            domain_rejected.domain_decision_receipt_ref,
        )

    def test_content_and_domain_unknown_reconcile_before_any_retry(self):
        content_identity = SubmissionIdentity(
            "fixture:agent/submission/content-unknown"
        )
        content_result, content_ledger = self.submit(
            fixture_idea_set(),
            submission=content_identity,
            content_reply=FixtureOwnerReply(
                status="outcome_unknown",
                receipt_ref="fixture:rm/receipt/content-unknown-observation",
                submission_ref=content_identity.ref,
            ),
            content_reconcile_reply=FixtureOwnerReply(
                status="accepted",
                receipt_ref="fixture:rm/receipt/reconciled-content",
                accepted_ref="fixture:rm/asset/reconciled-content",
                submission_ref=content_identity.ref,
            ),
        )
        self.assertEqual("accepted", content_result.status)
        self.assertEqual(
            (
                "fixture:rm/receipt/content-unknown-observation",
                "fixture:rm/receipt/reconciled-content",
            ),
            content_result.content_decision_receipt_refs,
        )
        self.assertLess(
            content_ledger.events.index(
                "ResearchMemory.reconcile_idea_outcome_content"
            ),
            content_ledger.events.index("ResearchGraph.submit_idea_outcome"),
        )

        registry = SubmissionIdentityRegistry()
        domain_identity = SubmissionIdentity(
            "fixture:agent/submission/domain-unknown"
        )
        unknown = FixtureOwnerReply(
            status="outcome_unknown",
            receipt_ref="fixture:rg/receipt/unknown-observation",
            submission_ref=domain_identity.ref,
        )
        first, first_ledger = self.submit(
            fixture_idea_set(),
            submission=domain_identity,
            registry=registry,
            domain_reply=unknown,
            domain_reconcile_reply=unknown,
        )
        self.assertEqual("outcome_unknown", first.status)
        self.assertIn("ResearchGraph.reconcile_idea_outcome", first_ledger.events)
        self.assertEqual(
            "fixture:rg/receipt/unknown-observation",
            first.domain_decision_receipt_ref,
        )

        reconciled, retry_ledger = self.submit(
            fixture_idea_set(),
            submission=domain_identity,
            registry=registry,
            domain_reconcile_reply=FixtureOwnerReply(
                status="accepted",
                receipt_ref="fixture:rg/receipt/reconciled-domain",
                accepted_ref="fixture:rg/idea-outcome/reconciled",
                submission_ref=domain_identity.ref,
            ),
        )
        self.assertEqual("accepted", reconciled.status)
        self.assertEqual(
            (
                "fixture:rg/receipt/unknown-observation",
                "fixture:rg/receipt/reconciled-domain",
            ),
            reconciled.domain_decision_receipt_refs,
        )
        self.assertIn("ResearchGraph.reconcile_idea_outcome", retry_ledger.events)
        self.assertNotIn(
            "ResearchMemory.accept_idea_outcome_content", retry_ledger.events
        )
        self.assertNotIn("ResearchGraph.submit_idea_outcome", retry_ledger.events)

    def test_revised_disposition_requires_an_actual_outcome_change(self):
        draft = fixture_idea_set()
        unchanged = fixture_review(draft)
        revised_disposition = ReviewDisposition(
            finding_id="f1",
            action="revised",
            rationale="The final outcome was revised in response to the finding.",
        )
        invalid = ReviewedOutcome(
            draft,
            draft,
            replace(unchanged.review, dispositions=(revised_disposition,)),
        )
        with self.assertRaises(ContractViolation):
            self.submit(draft, reviewed=invalid)

        final = fixture_idea_set(4)
        valid = ReviewedOutcome(
            draft,
            final,
            replace(
                unchanged.review,
                final_outcome_hash=canonical_hash(final),
                dispositions=(revised_disposition,),
            ),
        )
        result, _ = self.submit(final, reviewed=valid)
        self.assertEqual("accepted", result.status)

    def test_reconcile_technical_failure_cannot_degrade_into_blind_resubmit(self):
        for phase in ("content", "domain"):
            with self.subTest(phase=phase):
                registry = SubmissionIdentityRegistry()
                identity = SubmissionIdentity(
                    "fixture:agent/submission/{}-reconcile-failure".format(phase)
                )
                unknown = FixtureOwnerReply(
                    status="outcome_unknown",
                    receipt_ref="fixture:{}/receipt/unknown-before-failure".format(
                        "rm" if phase == "content" else "rg"
                    ),
                    submission_ref=identity.ref,
                )
                first_kwargs = {
                    "content_reply": unknown if phase == "content" else None,
                    "domain_reply": unknown if phase == "domain" else None,
                    "content_reconcile_failure": (
                        "content reconciliation Provider unavailable"
                        if phase == "content"
                        else None
                    ),
                    "domain_reconcile_failure": (
                        "domain reconciliation Provider unavailable"
                        if phase == "domain"
                        else None
                    ),
                }
                blocked, _ = self.submit(
                    fixture_idea_set(),
                    submission=identity,
                    registry=registry,
                    **first_kwargs,
                )
                self.assertEqual("technical_blocker", blocked.status)
                self.assertEqual(phase, blocked.reconciliation_required_phase)

                accepted_reply = FixtureOwnerReply(
                    status="accepted",
                    receipt_ref="fixture:{}/receipt/reconciled-after-recovery".format(
                        "rm" if phase == "content" else "rg"
                    ),
                    accepted_ref="fixture:{}/reconciled/after-recovery".format(
                        "rm" if phase == "content" else "rg"
                    ),
                    submission_ref=identity.ref,
                )
                retry_kwargs = {
                    "content_reconcile_reply": (
                        accepted_reply if phase == "content" else None
                    ),
                    "domain_reconcile_reply": (
                        accepted_reply if phase == "domain" else None
                    ),
                }
                recovered, ledger = self.submit(
                    fixture_idea_set(),
                    submission=identity,
                    registry=registry,
                    **retry_kwargs,
                )
                self.assertEqual("accepted", recovered.status)
                self.assertNotIn(
                    "ResearchMemory.accept_idea_outcome_content", ledger.events
                )
                if phase == "domain":
                    self.assertNotIn(
                        "ResearchGraph.submit_idea_outcome", ledger.events
                    )
                    self.assertIn(
                        "ResearchGraph.reconcile_idea_outcome", ledger.events
                    )
                else:
                    self.assertIn(
                        "ResearchMemory.reconcile_idea_outcome_content",
                        ledger.events,
                    )

    def test_stale_root_fence_stops_before_research_memory(self):
        ledger = CallLedger()
        with self.assertRaises(ContractViolation):
            submit_reviewed_outcome(
                self.request.ref,
                self.pack.ref,
                self.pack.content_sha256,
                SubmissionIdentity("fixture:agent/submission/stale-fence"),
                fixture_review(fixture_idea_set()),
                FakeInvocationPort(
                    ledger,
                    self.request,
                    self.pack,
                    observed_requests=(
                        self.request,
                        replace(self.request, root_fence_current=False),
                    ),
                ),
                FakeContentPort(ledger, accepted_content_reply()),
                FakeDomainPort(ledger, accepted_domain_reply()),
                FakeRuntimePort(ledger),
                SubmissionIdentityRegistry(),
            )
        self.assertNotIn("ResearchMemory.accept_idea_outcome_content", ledger.events)

    def test_stale_needs_input_and_unknown_remain_typed_non_outcomes(self):
        replies = (
            FixtureOwnerReply(
                status="stale", receipt_ref="fixture:rg/receipt/stale"
            ),
            FixtureOwnerReply(
                status="needs_input",
                receipt_ref="fixture:rg/receipt/needs-input",
                human_request_ref="fixture:hc/human-request/1",
            ),
            FixtureOwnerReply(
                status="outcome_unknown",
                receipt_ref="fixture:rg/receipt/unknown-typed-state",
                submission_ref="fixture:agent/submission/test-1",
            ),
        )
        for reply in replies:
            with self.subTest(status=reply.status):
                result, ledger = self.submit(
                    fixture_idea_set(), domain_reply=reply
                )
                self.assertEqual(reply.status, result.status)
                self.assertFalse(result.simulated_domain_accepted)
                self.assertFalse(result.is_stage_advanced)
                self.assertNotIn(
                    "AdvancementEngine.submit_exhaustion_proposal", ledger.events
                )

    def test_stale_exact_replay_reobserves_runtime_before_content_write(self):
        registry = SubmissionIdentityRegistry()
        identity = SubmissionIdentity("fixture:agent/submission/stale-replay")
        stale, _ = self.submit(
            fixture_idea_set(),
            submission=identity,
            registry=registry,
            domain_reply=FixtureOwnerReply(
                status="stale", receipt_ref="fixture:rg/receipt/stale-replay"
            ),
        )
        self.assertEqual("stale", stale.status)
        replay, ledger = self.submit(
            fixture_idea_set(), submission=identity, registry=registry
        )
        self.assertEqual("accepted", replay.status)
        self.assertLess(
            ledger.events.index("AgentRuntime.observe_run"),
            ledger.events.index("ResearchGraph.submit_idea_outcome"),
        )
        self.assertNotIn(
            "ResearchMemory.accept_idea_outcome_content", ledger.events
        )

        replacement_registry = SubmissionIdentityRegistry()
        replacement_old = SubmissionIdentity(
            "fixture:agent/submission/stale-before-change"
        )
        stale_before_change, _ = self.submit(
            fixture_idea_set(),
            submission=replacement_old,
            registry=replacement_registry,
            domain_reply=FixtureOwnerReply(
                status="stale",
                receipt_ref="fixture:rg/receipt/stale-before-change",
            ),
        )
        self.assertEqual("stale", stale_before_change.status)
        with self.assertRaises(ContractViolation):
            self.submit(
                fixture_idea_set(),
                submission=SubmissionIdentity(
                    "fixture:agent/submission/stale-new-id-unchanged"
                ),
                registry=replacement_registry,
            )
        changed, _ = self.submit(
            fixture_idea_set(4),
            submission=SubmissionIdentity(
                "fixture:agent/submission/stale-new-payload"
            ),
            registry=replacement_registry,
        )
        self.assertEqual("accepted", changed.status)

    def test_superseded_recoverable_identity_cannot_replay(self):
        stale_registry = SubmissionIdentityRegistry()
        stale_identity = SubmissionIdentity(
            "fixture:agent/submission/superseded-stale"
        )
        self.submit(
            fixture_idea_set(),
            submission=stale_identity,
            registry=stale_registry,
            domain_reply=FixtureOwnerReply(
                status="stale",
                receipt_ref="fixture:rg/receipt/superseded-stale",
            ),
        )
        stale_replacement = SubmissionIdentity(
            "fixture:agent/submission/stale-replacement-head"
        )
        self.submit(
            fixture_idea_set(4),
            submission=stale_replacement,
            registry=stale_registry,
        )
        with self.assertRaises(ContractViolation):
            self.submit(
                fixture_idea_set(),
                submission=stale_identity,
                registry=stale_registry,
            )

        technical_registry = SubmissionIdentityRegistry()
        technical_identity = SubmissionIdentity(
            "fixture:agent/submission/superseded-technical"
        )
        self.submit(
            fixture_idea_set(),
            submission=technical_identity,
            registry=technical_registry,
            content_failure="definite no-write failure",
        )
        technical_replacement = SubmissionIdentity(
            "fixture:agent/submission/technical-replacement-head"
        )
        self.submit(
            fixture_idea_set(4),
            submission=technical_replacement,
            registry=technical_registry,
        )
        with self.assertRaises(ContractViolation):
            self.submit(
                fixture_idea_set(),
                submission=technical_identity,
                registry=technical_registry,
            )

        needs_registry = SubmissionIdentityRegistry()
        needs_identity = SubmissionIdentity(
            "fixture:agent/submission/superseded-needs-input"
        )
        needs_result, _ = self.submit(
            fixture_idea_set(),
            submission=needs_identity,
            registry=needs_registry,
            domain_reply=FixtureOwnerReply(
                status="needs_input",
                receipt_ref="fixture:rg/receipt/superseded-needs-input",
                human_request_ref="fixture:hc/human-request/superseded",
            ),
        )
        recovery_observation = FixtureRunObservation(
            run_ref=self.request.run_ref,
            root_session_ref=self.request.root_session_ref,
            execution_fence_ref=self.request.execution_fence_ref,
            recovery_dispositions=(
                FixtureRecoveryDisposition(
                    human_request_ref="fixture:hc/human-request/superseded",
                    owner_recovery_receipt_ref=(
                        "fixture:hc/receipt/superseded-recovered"
                    ),
                ),
            ),
        )
        needs_replacement = SubmissionIdentity(
            "fixture:agent/submission/needs-input-replacement-head",
            predecessor_submission_ref=needs_identity.ref,
            owner_needs_input_receipt_ref=(
                needs_result.domain_decision_receipt_ref
            ),
            owner_recovery_receipt_ref=(
                "fixture:hc/receipt/superseded-recovered"
            ),
        )
        self.submit(
            fixture_idea_set(4),
            submission=needs_replacement,
            registry=needs_registry,
            runtime_observation=recovery_observation,
        )
        with self.assertRaises(ContractViolation):
            self.submit(
                fixture_idea_set(),
                submission=needs_identity,
                registry=needs_registry,
                runtime_observation=recovery_observation,
            )

    def test_needs_input_without_human_request_fails_closed(self):
        reply = FixtureOwnerReply(
            status="needs_input", receipt_ref="fixture:rg/receipt/bad-needs-input"
        )
        with self.assertRaises(ContractViolation):
            self.submit(fixture_idea_set(), domain_reply=reply)

    def test_unknown_without_observation_receipt_fails_closed(self):
        reply = FixtureOwnerReply(
            status="outcome_unknown",
            submission_ref="fixture:agent/submission/unreceipted-unknown",
        )
        with self.assertRaises(ContractViolation):
            reply.validate()

    def test_needs_input_recovers_only_with_exact_owner_disposition(self):
        registry = SubmissionIdentityRegistry()
        identity = SubmissionIdentity(
            "fixture:agent/submission/needs-input-recovery"
        )
        needs_input, _ = self.submit(
            fixture_idea_set(),
            submission=identity,
            registry=registry,
            domain_reply=FixtureOwnerReply(
                status="needs_input",
                receipt_ref="fixture:rg/receipt/needs-input-recovery",
                human_request_ref="fixture:hc/human-request/recovery-1",
            ),
        )
        self.assertEqual("needs_input", needs_input.status)

        still_waiting, waiting_ledger = self.submit(
            fixture_idea_set(), submission=identity, registry=registry
        )
        self.assertEqual("needs_input", still_waiting.status)
        self.assertNotIn(
            "ResearchGraph.submit_idea_outcome", waiting_ledger.events
        )

        recovered_observation = FixtureRunObservation(
            run_ref=self.request.run_ref,
            root_session_ref=self.request.root_session_ref,
            execution_fence_ref=self.request.execution_fence_ref,
            recovery_dispositions=(
                FixtureRecoveryDisposition(
                    human_request_ref="fixture:hc/human-request/recovery-1",
                    owner_recovery_receipt_ref=(
                        "fixture:hc/receipt/recovery-disposition-1"
                    ),
                ),
            ),
        )
        recovered, recovered_ledger = self.submit(
            fixture_idea_set(),
            submission=identity,
            registry=registry,
            runtime_observation=recovered_observation,
        )
        self.assertEqual("accepted", recovered.status)
        self.assertNotIn(
            "ResearchMemory.accept_idea_outcome_content", recovered_ledger.events
        )
        self.assertIn("ResearchGraph.submit_idea_outcome", recovered_ledger.events)
        self.assertEqual(
            (
                "fixture:rg/receipt/needs-input-recovery",
                "fixture:rg/receipt/outcome-test",
            ),
            recovered.domain_decision_receipt_refs,
        )
        self.assertEqual(
            ("fixture:hc/human-request/recovery-1",),
            recovered.human_request_refs,
        )
        self.assertEqual(
            ("fixture:hc/receipt/recovery-disposition-1",),
            recovered.owner_recovery_receipt_refs,
        )

    def test_changed_payload_after_needs_input_links_recovery_lineage(self):
        registry = SubmissionIdentityRegistry()
        predecessor = SubmissionIdentity(
            "fixture:agent/submission/needs-input-before-change"
        )
        needs_input, _ = self.submit(
            fixture_idea_set(),
            submission=predecessor,
            registry=registry,
            domain_reply=FixtureOwnerReply(
                status="needs_input",
                receipt_ref="fixture:rg/receipt/needs-input-before-change",
                human_request_ref="fixture:hc/human-request/change-1",
            ),
        )
        observation = FixtureRunObservation(
            run_ref=self.request.run_ref,
            root_session_ref=self.request.root_session_ref,
            execution_fence_ref=self.request.execution_fence_ref,
            recovery_dispositions=(
                FixtureRecoveryDisposition(
                    human_request_ref="fixture:hc/human-request/change-1",
                    owner_recovery_receipt_ref=(
                        "fixture:hc/receipt/change-recovered"
                    ),
                ),
            ),
        )
        with self.assertRaises(ContractViolation):
            self.submit(
                fixture_idea_set(4),
                submission=SubmissionIdentity(
                    "fixture:agent/submission/unlinked-needs-input-change"
                ),
                registry=registry,
                runtime_observation=observation,
            )
        successor = SubmissionIdentity(
            "fixture:agent/submission/linked-needs-input-change",
            predecessor_submission_ref=predecessor.ref,
            owner_needs_input_receipt_ref=(
                needs_input.domain_decision_receipt_ref
            ),
            owner_recovery_receipt_ref="fixture:hc/receipt/change-recovered",
        )
        changed, _ = self.submit(
            fixture_idea_set(4),
            submission=successor,
            registry=registry,
            runtime_observation=observation,
        )
        self.assertEqual("accepted", changed.status)

    def test_plain_technical_recovery_allows_only_changed_new_identity(self):
        registry = SubmissionIdentityRegistry()
        predecessor = SubmissionIdentity(
            "fixture:agent/submission/plain-technical-before-change"
        )
        blocked, _ = self.submit(
            fixture_idea_set(),
            submission=predecessor,
            registry=registry,
            content_failure="definite no-write Provider failure",
        )
        self.assertEqual("technical_blocker", blocked.status)
        self.assertIsNone(blocked.reconciliation_required_phase)
        with self.assertRaises(ContractViolation):
            self.submit(
                fixture_idea_set(),
                submission=SubmissionIdentity(
                    "fixture:agent/submission/plain-technical-same-payload"
                ),
                registry=registry,
            )
        changed, _ = self.submit(
            fixture_idea_set(4),
            submission=SubmissionIdentity(
                "fixture:agent/submission/plain-technical-changed-payload"
            ),
            registry=registry,
        )
        self.assertEqual("accepted", changed.status)

    def test_content_checkpoint_survives_domain_prewrite_failure(self):
        registry = SubmissionIdentityRegistry()
        identity = SubmissionIdentity(
            "fixture:agent/submission/content-checkpoint"
        )
        ready = FixtureRunObservation(
            run_ref=self.request.run_ref,
            root_session_ref=self.request.root_session_ref,
            execution_fence_ref=self.request.execution_fence_ref,
        )
        blocked = replace(
            ready,
            technical_blocker_refs=("fixture:ar/blocker/domain-prewrite",),
        )
        ledger = CallLedger()
        with self.assertRaises(ContractViolation):
            submit_reviewed_outcome(
                self.request.ref,
                self.pack.ref,
                self.pack.content_sha256,
                identity,
                fixture_review(fixture_idea_set()),
                FakeInvocationPort(ledger, self.request, self.pack),
                FakeContentPort(ledger, accepted_content_reply()),
                FakeDomainPort(ledger, accepted_domain_reply()),
                FakeRuntimePort(ledger, observations=(ready, blocked)),
                registry,
            )
        self.assertIsNone(registry.records[identity.ref].result)
        self.assertEqual(
            "fixture:rm/asset/idea-content-test",
            registry.records[identity.ref].content_ref,
        )

        drifting_content_reply = FixtureOwnerReply(
            status="accepted",
            receipt_ref="fixture:rm/receipt/content-drift-must-not-be-used",
            accepted_ref="fixture:rm/asset/content-drift-must-not-be-used",
        )
        recovered, recovered_ledger = self.submit(
            fixture_idea_set(),
            submission=identity,
            registry=registry,
            content_reply=drifting_content_reply,
        )
        self.assertEqual("accepted", recovered.status)
        self.assertEqual(
            "fixture:rm/asset/idea-content-test", recovered.content_ref
        )
        self.assertNotIn(
            "ResearchMemory.accept_idea_outcome_content", recovered_ledger.events
        )

    def test_terminal_registry_result_cannot_be_overwritten(self):
        registry = SubmissionIdentityRegistry()
        identity = SubmissionIdentity(
            "fixture:agent/submission/immutable-terminal"
        )
        accepted, _ = self.submit(
            fixture_idea_set(), submission=identity, registry=registry
        )
        forged = SubmissionResult(
            status="rejected",
            decision_phase="domain",
            content_ref=accepted.content_ref,
            content_decision_receipt_ref=(
                accepted.content_decision_receipt_ref
            ),
            domain_decision_receipt_ref="fixture:rg/receipt/forged-rejection",
            content_decision_receipt_refs=(
                accepted.content_decision_receipt_refs
            ),
            domain_decision_receipt_refs=(
                accepted.domain_decision_receipt_refs
                + ("fixture:rg/receipt/forged-rejection",)
            ),
            feedback=("forged",),
            submission_ref=identity.ref,
        )
        with self.assertRaises(ContractViolation):
            registry.record_result(identity, forged)

    def test_recoverable_registry_result_requires_internal_transition_proof(self):
        registry = SubmissionIdentityRegistry()
        identity = SubmissionIdentity(
            "fixture:agent/submission/proof-required"
        )
        stale, _ = self.submit(
            fixture_idea_set(),
            submission=identity,
            registry=registry,
            domain_reply=FixtureOwnerReply(
                status="stale",
                receipt_ref="fixture:rg/receipt/proof-required-stale",
            ),
        )
        forged = replace(
            stale,
            status="accepted",
            outcome_ref="fixture:rg/idea-outcome/forged",
            domain_decision_receipt_ref="fixture:rg/receipt/forged-accepted",
            domain_decision_receipt_refs=(
                stale.domain_decision_receipt_refs
                + ("fixture:rg/receipt/forged-accepted",)
            ),
            simulated_domain_accepted=True,
        )
        with self.assertRaises(ContractViolation):
            registry.record_result(identity, forged)

    def test_repeated_technical_blockers_retain_history(self):
        registry = SubmissionIdentityRegistry()
        identity = SubmissionIdentity(
            "fixture:agent/submission/repeated-technical"
        )
        first, _ = self.submit(
            fixture_idea_set(),
            submission=identity,
            registry=registry,
            content_failure="first definite no-write failure",
            runtime_reported_blocker_refs=("fixture:ar/blocker/first",),
        )
        self.assertEqual(("fixture:ar/blocker/first",), first.blocker_refs)
        second, _ = self.submit(
            fixture_idea_set(),
            submission=identity,
            registry=registry,
            content_failure="second definite no-write failure",
            runtime_reported_blocker_refs=("fixture:ar/blocker/second",),
        )
        self.assertEqual("technical_blocker", second.status)
        self.assertEqual(
            ("fixture:ar/blocker/first", "fixture:ar/blocker/second"),
            second.blocker_refs,
        )

    def test_technical_failure_reports_runtime_blocker_only(self):
        result, ledger = self.submit(
            fixture_idea_set(), content_failure="fixture Provider unavailable"
        )
        self.assertEqual("technical_blocker", result.status)
        self.assertEqual("fixture:ar/blocker/1", result.blocker_ref)
        self.assertEqual(
            [
                "AdvancementEngine.observe_idea_stage_run",
                "ProjectionStore.read_frozen_context_pack",
                "AgentRuntime.observe_run",
                "AdvancementEngine.observe_idea_stage_run",
                "ResearchMemory.accept_idea_outcome_content",
                "AgentRuntime.report_execution_blocker",
            ],
            ledger.events,
        )
        self.assertNotIn("ResearchGraph.submit_idea_outcome", ledger.events)
        self.assertNotIn(
            "AdvancementEngine.submit_exhaustion_proposal", ledger.events
        )
        self.assert_no_forbidden_authority(ledger)

        domain_result, domain_ledger = self.submit(
            fixture_idea_set(), domain_failure="fixture domain Provider unavailable"
        )
        self.assertEqual("technical_blocker", domain_result.status)
        self.assertEqual("domain", domain_result.decision_phase)
        self.assertEqual(
            "fixture:rm/receipt/content-test",
            domain_result.content_decision_receipt_ref,
        )
        self.assertEqual(
            "fixture:rm/asset/idea-content-test", domain_result.content_ref
        )
        self.assertIn("ResearchGraph.submit_idea_outcome", domain_ledger.events)
        self.assertIn("AgentRuntime.report_execution_blocker", domain_ledger.events)

    def test_unresolved_runtime_fact_blocks_formal_owner_write(self):
        observation = FixtureRunObservation(
            run_ref=self.request.run_ref,
            root_session_ref=self.request.root_session_ref,
            execution_fence_ref=self.request.execution_fence_ref,
            technical_blocker_refs=("fixture:ar/blocker/existing",),
        )
        with self.assertRaises(ContractViolation):
            self.submit(
                fixture_idea_set(), runtime_observation=observation
            )

    def test_clean_exhaustion_submits_only_non_authoritative_proposal(self):
        ledger = CallLedger()
        closure = ExhaustionClosure(
            exploration_record_refs=("fixture:agent/exploration/1",),
            prior_submission_refs=(),
            owner_rejection_receipt_refs=(),
            cannot_form_idea_set_reason="No materially distinct candidate remains.",
            cannot_form_no_viable_reason=(
                "Available evidence cannot support a bounded negative outcome."
            ),
        )
        result = submit_exhaustion_proposal(
            self.request.ref,
            self.pack.ref,
            self.pack.content_sha256,
            closure,
            FakeInvocationPort(ledger, self.request, self.pack),
            FakeRuntimePort(ledger),
            FakeAdvancementPort(
                ledger,
                FixtureOwnerReply(
                    status="accepted",
                    receipt_ref="fixture:ae/receipt/proposal-accepted",
                    accepted_ref="fixture:ae/proposal/exhaustion-1",
                    submission_ref="fixture:ae/proposal/exhaustion-1",
                ),
            ),
        )
        self.assertEqual("exhaustion_proposal_accepted", result.status)
        self.assertEqual(
            "fixture:ae/proposal/exhaustion-1",
            result.exhaustion_proposal_ref,
        )
        self.assertEqual(
            [
                "AdvancementEngine.observe_idea_stage_run",
                "ProjectionStore.read_frozen_context_pack",
                "AgentRuntime.observe_run",
                "AdvancementEngine.observe_idea_stage_run",
                "AdvancementEngine.submit_exhaustion_proposal",
            ],
            ledger.events,
        )
        self.assertFalse(result.is_stage_advanced)
        self.assertFalse(result.stage_commit_created_by_skill)
        self.assert_no_forbidden_authority(ledger)

    def test_exhaustion_nonaccepted_owner_results_remain_typed(self):
        replies = (
            FixtureOwnerReply(
                status="rejected",
                receipt_ref="fixture:ae/receipt/exhaustion-rejected",
                feedback=("Exploration evidence is incomplete.",),
            ),
            FixtureOwnerReply(
                status="stale",
                receipt_ref="fixture:ae/receipt/exhaustion-stale",
            ),
            FixtureOwnerReply(
                status="needs_input",
                receipt_ref="fixture:ae/receipt/exhaustion-needs-input",
                human_request_ref="fixture:hc/human-request/exhaustion-1",
            ),
        )
        for reply in replies:
            with self.subTest(status=reply.status):
                ledger = CallLedger()
                result = submit_exhaustion_proposal(
                    self.request.ref,
                    self.pack.ref,
                    self.pack.content_sha256,
                    self.clean_exhaustion_closure(),
                    FakeInvocationPort(ledger, self.request, self.pack),
                    FakeRuntimePort(ledger),
                    FakeAdvancementPort(ledger, reply),
                )
                self.assertEqual(
                    "exhaustion_proposal_" + reply.status,
                    result.status,
                )
                self.assertEqual(reply.feedback, result.feedback)
                self.assertEqual(
                    reply.human_request_ref, result.human_request_ref
                )
                result.validate()
                self.assertFalse(result.is_stage_advanced)

    def test_exhaustion_unknown_reconciles_same_submission_identity(self):
        unknown = FixtureOwnerReply(
            status="outcome_unknown",
            receipt_ref="fixture:ae/receipt/exhaustion-unknown",
            submission_ref="fixture:ae/submission/exhaustion-unknown",
        )
        ledger = CallLedger()
        resolved = submit_exhaustion_proposal(
            self.request.ref,
            self.pack.ref,
            self.pack.content_sha256,
            self.clean_exhaustion_closure(),
            FakeInvocationPort(ledger, self.request, self.pack),
            FakeRuntimePort(ledger),
            FakeAdvancementPort(
                ledger,
                unknown,
                reconcile_reply=FixtureOwnerReply(
                    status="accepted",
                    receipt_ref="fixture:ae/receipt/exhaustion-resolved",
                    accepted_ref="fixture:ae/proposal/exhaustion-resolved",
                    submission_ref=unknown.submission_ref,
                ),
            ),
        )
        self.assertEqual("exhaustion_proposal_accepted", resolved.status)
        self.assertEqual(
            (
                "fixture:ae/receipt/exhaustion-unknown",
                "fixture:ae/receipt/exhaustion-resolved",
            ),
            resolved.advancement_decision_receipt_refs,
        )
        self.assertEqual(unknown.submission_ref, resolved.submission_ref)
        self.assertIn(
            "AdvancementEngine.reconcile_exhaustion_proposal", ledger.events
        )
        resolved.validate()

        still_unknown_ledger = CallLedger()
        still_unknown = submit_exhaustion_proposal(
            self.request.ref,
            self.pack.ref,
            self.pack.content_sha256,
            self.clean_exhaustion_closure(),
            FakeInvocationPort(still_unknown_ledger, self.request, self.pack),
            FakeRuntimePort(still_unknown_ledger),
            FakeAdvancementPort(still_unknown_ledger, unknown),
        )
        self.assertEqual(
            "exhaustion_proposal_outcome_unknown", still_unknown.status
        )
        self.assertEqual(
            "advancement", still_unknown.reconciliation_required_phase
        )
        still_unknown.validate()

        forged_without_unknown_receipt = replace(
            still_unknown,
            status="technical_blocker",
            advancement_decision_receipt_ref=None,
            advancement_decision_receipt_refs=(),
            blocker_ref="fixture:ar/blocker/forged-reconcile",
            blocker_refs=("fixture:ar/blocker/forged-reconcile",),
        )
        with self.assertRaises(ContractViolation):
            forged_without_unknown_receipt.validate()

        other_request_ref = "fixture:ae/stage-run-request/idea-cross-run"
        other_pack = make_context_pack(
            ref="fixture:projection/context-pack/idea-cross-run",
            request_ref=other_request_ref,
            question_ref=self.request.question_ref,
            literature_revision_ref=None,
            items=self.pack.items,
        )
        other_request = replace(
            self.request,
            ref=other_request_ref,
            run_ref="fixture:ar/run/idea-cross-run",
            root_session_ref="fixture:ar/session/root-cross-run",
            execution_fence_ref="fixture:ar/fence/idea-cross-run",
            context_pack_ref=other_pack.ref,
            context_pack_sha256=other_pack.content_sha256,
        )
        cross_run_ledger = CallLedger()
        with self.assertRaises(ContractViolation):
            reconcile_exhaustion_proposal(
                other_request.ref,
                other_pack.ref,
                other_pack.content_sha256,
                still_unknown,
                FakeInvocationPort(
                    cross_run_ledger, other_request, other_pack
                ),
                FakeRuntimePort(cross_run_ledger),
                FakeAdvancementPort(
                    cross_run_ledger,
                    unknown,
                    reconcile_reply=FixtureOwnerReply(
                        status="accepted",
                        receipt_ref="fixture:ae/receipt/cross-run-must-not-accept",
                        accepted_ref="fixture:ae/proposal/cross-run-must-not-accept",
                        submission_ref=unknown.submission_ref,
                    ),
                ),
            )
        self.assertNotIn(
            "AdvancementEngine.reconcile_exhaustion_proposal",
            cross_run_ledger.events,
        )

        recovery_ledger = CallLedger()
        recovered = reconcile_exhaustion_proposal(
            self.request.ref,
            self.pack.ref,
            self.pack.content_sha256,
            still_unknown,
            FakeInvocationPort(recovery_ledger, self.request, self.pack),
            FakeRuntimePort(recovery_ledger),
            FakeAdvancementPort(
                recovery_ledger,
                unknown,
                reconcile_reply=FixtureOwnerReply(
                    status="accepted",
                    receipt_ref="fixture:ae/receipt/exhaustion-later-resolved",
                    accepted_ref="fixture:ae/proposal/exhaustion-later-resolved",
                    submission_ref=unknown.submission_ref,
                ),
            ),
        )
        self.assertEqual("exhaustion_proposal_accepted", recovered.status)
        self.assertNotIn(
            "AdvancementEngine.submit_exhaustion_proposal",
            recovery_ledger.events,
        )
        self.assertIn(
            "AdvancementEngine.reconcile_exhaustion_proposal",
            recovery_ledger.events,
        )

    def test_exhaustion_technical_failures_route_to_runtime_blocker(self):
        ledger = CallLedger()
        blocked = submit_exhaustion_proposal(
            self.request.ref,
            self.pack.ref,
            self.pack.content_sha256,
            self.clean_exhaustion_closure(),
            FakeInvocationPort(ledger, self.request, self.pack),
            FakeRuntimePort(
                ledger,
                reported_blocker_refs=(
                    "fixture:ar/blocker/exhaustion-submit",
                ),
            ),
            FakeAdvancementPort(
                ledger,
                FixtureOwnerReply(
                    status="accepted",
                    receipt_ref="fixture:ae/receipt/must-not-exist",
                    accepted_ref="fixture:ae/proposal/must-not-exist",
                ),
                technical_failure="Advancement Provider unavailable",
            ),
        )
        self.assertEqual("technical_blocker", blocked.status)
        self.assertEqual(
            ("fixture:ar/blocker/exhaustion-submit",), blocked.blocker_refs
        )
        self.assertIsNone(blocked.reconciliation_required_phase)
        blocked.validate()

        unknown = FixtureOwnerReply(
            status="outcome_unknown",
            receipt_ref="fixture:ae/receipt/exhaustion-before-failure",
            submission_ref="fixture:ae/submission/exhaustion-before-failure",
        )
        reconcile_ledger = CallLedger()
        reconcile_blocked = submit_exhaustion_proposal(
            self.request.ref,
            self.pack.ref,
            self.pack.content_sha256,
            self.clean_exhaustion_closure(),
            FakeInvocationPort(reconcile_ledger, self.request, self.pack),
            FakeRuntimePort(
                reconcile_ledger,
                reported_blocker_refs=(
                    "fixture:ar/blocker/exhaustion-reconcile",
                ),
            ),
            FakeAdvancementPort(
                reconcile_ledger,
                unknown,
                reconcile_technical_failure=(
                    "Advancement reconciliation Provider unavailable"
                ),
            ),
        )
        self.assertEqual("technical_blocker", reconcile_blocked.status)
        self.assertEqual(
            "advancement", reconcile_blocked.reconciliation_required_phase
        )
        self.assertEqual(
            ("fixture:ae/receipt/exhaustion-before-failure",),
            reconcile_blocked.advancement_decision_receipt_refs,
        )
        self.assertEqual(unknown.submission_ref, reconcile_blocked.submission_ref)
        reconcile_blocked.validate()

        recovery_ledger = CallLedger()
        recovered = reconcile_exhaustion_proposal(
            self.request.ref,
            self.pack.ref,
            self.pack.content_sha256,
            reconcile_blocked,
            FakeInvocationPort(recovery_ledger, self.request, self.pack),
            FakeRuntimePort(recovery_ledger),
            FakeAdvancementPort(
                recovery_ledger,
                unknown,
                reconcile_reply=FixtureOwnerReply(
                    status="accepted",
                    receipt_ref=(
                        "fixture:ae/receipt/exhaustion-after-technical"
                    ),
                    accepted_ref=(
                        "fixture:ae/proposal/exhaustion-after-technical"
                    ),
                    submission_ref=unknown.submission_ref,
                ),
            ),
        )
        self.assertEqual("exhaustion_proposal_accepted", recovered.status)
        self.assertEqual(
            ("fixture:ar/blocker/exhaustion-reconcile",),
            recovered.blocker_refs,
        )
        self.assertNotIn(
            "AdvancementEngine.submit_exhaustion_proposal",
            recovery_ledger.events,
        )

    def test_exhaustion_does_not_require_prior_submission_or_rejection(self):
        verified = verify_invocation(self.request, self.pack)
        closure = ExhaustionClosure(
            exploration_record_refs=("fixture:agent/exploration/no-submit",),
            prior_submission_refs=(),
            owner_rejection_receipt_refs=(),
            cannot_form_idea_set_reason="No candidate is responsibly submit-ready.",
            cannot_form_no_viable_reason="Evidence is insufficient for a negative outcome.",
        )
        proposal = build_exhaustion_proposal(verified, closure)
        self.assertEqual((), proposal.closure.prior_submission_refs)
        self.assertEqual((), proposal.closure.owner_rejection_receipt_refs)

        stale_history = replace(
            closure,
            prior_submission_refs=("fixture:agent/submission/resolved-stale",),
        )
        stale_proposal = build_exhaustion_proposal(verified, stale_history)
        self.assertEqual(
            ("fixture:agent/submission/resolved-stale",),
            stale_proposal.closure.prior_submission_refs,
        )
        self.assertEqual(
            (), stale_proposal.closure.owner_rejection_receipt_refs
        )

    def test_live_run_observation_can_block_a_clean_claimed_exhaustion(self):
        ledger = CallLedger()
        closure = ExhaustionClosure(
            exploration_record_refs=("fixture:agent/exploration/claimed-clean",),
            prior_submission_refs=(),
            owner_rejection_receipt_refs=(),
            cannot_form_idea_set_reason="No distinct candidate remains.",
            cannot_form_no_viable_reason="A negative outcome is not supportable.",
        )
        observation = FixtureRunObservation(
            run_ref=self.request.run_ref,
            root_session_ref=self.request.root_session_ref,
            execution_fence_ref=self.request.execution_fence_ref,
            outcome_unknown_refs=("fixture:agent/submission/still-unknown",),
        )
        with self.assertRaises(ContractViolation):
            submit_exhaustion_proposal(
                self.request.ref,
                self.pack.ref,
                self.pack.content_sha256,
                closure,
                FakeInvocationPort(ledger, self.request, self.pack),
                FakeRuntimePort(ledger, observation),
                FakeAdvancementPort(
                    ledger,
                    FixtureOwnerReply(
                        status="accepted",
                        receipt_ref="fixture:ae/receipt/must-not-submit",
                        accepted_ref="fixture:ae/proposal/must-not-submit",
                    ),
                ),
            )
        self.assertIn("AgentRuntime.observe_run", ledger.events)
        self.assertNotIn(
            "AdvancementEngine.submit_exhaustion_proposal", ledger.events
        )

    def test_exhaustion_history_must_match_live_runtime_observation(self):
        ledger = CallLedger()
        closure = ExhaustionClosure(
            exploration_record_refs=("fixture:agent/exploration/history",),
            prior_submission_refs=(),
            owner_rejection_receipt_refs=(),
            cannot_form_idea_set_reason="No materially distinct direction remains.",
            cannot_form_no_viable_reason="A bounded negative claim is unsupported.",
        )
        observation = FixtureRunObservation(
            run_ref=self.request.run_ref,
            root_session_ref=self.request.root_session_ref,
            execution_fence_ref=self.request.execution_fence_ref,
            prior_submission_refs=(
                "fixture:agent/submission/rejected-before-exhaustion",
            ),
            owner_rejection_receipt_refs=(
                "fixture:rg/receipt/rejected-before-exhaustion",
            ),
        )
        with self.assertRaises(ContractViolation):
            submit_exhaustion_proposal(
                self.request.ref,
                self.pack.ref,
                self.pack.content_sha256,
                closure,
                FakeInvocationPort(ledger, self.request, self.pack),
                FakeRuntimePort(ledger, observation),
                FakeAdvancementPort(
                    ledger,
                    FixtureOwnerReply(
                        status="accepted",
                        receipt_ref="fixture:ae/receipt/missing-history",
                        accepted_ref="fixture:ae/proposal/missing-history",
                    ),
                ),
            )
        self.assertNotIn(
            "AdvancementEngine.submit_exhaustion_proposal", ledger.events
        )

    def test_every_unresolved_gate_blocks_exhaustion_before_port_call(self):
        base = ExhaustionClosure(
            exploration_record_refs=("fixture:agent/exploration/1",),
            prior_submission_refs=("fixture:agent/submission/1",),
            owner_rejection_receipt_refs=("fixture:rg/receipt/rejection-1",),
            cannot_form_idea_set_reason="No materially distinct direction remains.",
            cannot_form_no_viable_reason="A negative conclusion is not supportable.",
        )
        blocked = (
            replace(
                base,
                pending_submission_refs=("fixture:agent/submission/pending",),
            ),
            replace(
                base,
                accepted_unconsumed_outcome_refs=(
                    "fixture:rg/outcome/accepted-unconsumed",
                ),
            ),
            replace(
                base, human_request_refs=("fixture:hc/human-request/1",)
            ),
            replace(
                base, technical_blocker_refs=("fixture:ar/blocker/1",)
            ),
            replace(
                base, outcome_unknown_refs=("fixture:agent/submission/unknown",)
            ),
            replace(base, existing_stage_commit_ref="fixture:ae/stage-commit/1"),
            replace(base, defensible_idea_set_available=True),
            replace(base, defensible_no_viable_available=True),
            replace(base, run_reconciled=False),
        )
        for closure in blocked:
            with self.subTest(closure=closure):
                ledger = CallLedger()
                with self.assertRaises(ContractViolation):
                    submit_exhaustion_proposal(
                        self.request.ref,
                        self.pack.ref,
                        self.pack.content_sha256,
                        closure,
                        FakeInvocationPort(ledger, self.request, self.pack),
                        FakeRuntimePort(
                            ledger,
                            FixtureRunObservation(
                                run_ref=self.request.run_ref,
                                root_session_ref=self.request.root_session_ref,
                                execution_fence_ref=(
                                    self.request.execution_fence_ref
                                ),
                                prior_submission_refs=(
                                    closure.prior_submission_refs
                                ),
                                owner_rejection_receipt_refs=(
                                    closure.owner_rejection_receipt_refs
                                ),
                                pending_submission_refs=closure.pending_submission_refs,
                                accepted_unconsumed_outcome_refs=(
                                    closure.accepted_unconsumed_outcome_refs
                                ),
                                human_request_refs=closure.human_request_refs,
                                technical_blocker_refs=closure.technical_blocker_refs,
                                outcome_unknown_refs=closure.outcome_unknown_refs,
                                existing_stage_commit_ref=(
                                    closure.existing_stage_commit_ref
                                ),
                                run_reconciled=closure.run_reconciled,
                            ),
                        ),
                        FakeAdvancementPort(
                            ledger,
                            FixtureOwnerReply(
                                status="accepted",
                                receipt_ref="fixture:ae/receipt/should-not-exist",
                                accepted_ref="fixture:ae/proposal/should-not-exist",
                            ),
                        ),
                    )
                self.assertNotIn(
                    "AdvancementEngine.submit_exhaustion_proposal", ledger.events
                )

    def test_accepted_no_viable_explicitly_blocks_exhaustion(self):
        accepted, _ = self.submit(fixture_no_viable())
        self.assertTrue(accepted.simulated_domain_accepted)
        self.assertFalse(accepted.is_owner_fact)
        verified = verify_invocation(self.request, self.pack)
        closure = ExhaustionClosure(
            exploration_record_refs=("fixture:agent/exploration/1",),
            prior_submission_refs=("fixture:agent/submission/nvc",),
            owner_rejection_receipt_refs=(),
            cannot_form_idea_set_reason="No positive candidate remains.",
            cannot_form_no_viable_reason="This is false because NVC was accepted.",
            defensible_no_viable_available=True,
        )
        with self.assertRaises(ContractViolation):
            build_exhaustion_proposal(verified, closure)


if __name__ == "__main__":
    unittest.main(verbosity=2)
