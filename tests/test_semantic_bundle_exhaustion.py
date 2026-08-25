from __future__ import annotations

from meta_research.bundle_exhaustion import (
    BUNDLE_EXHAUSTION_EVIDENCE_RECEIPT_KIND,
    BundleExhaustionOperationResult,
    BundleExhaustionProposal,
)
from meta_research.owners.common import AcceptanceReceipt
from meta_research.semantic_owner_gateway import create_semantic_owner_gateway
from test_semantic_bundle_target_catalog import (
    _FakeAdvancementEngine,
    _FakeAgentRuntime,
    _FakeResearchGraph,
    _FakeResearchMemory,
    _call,
    _issue,
    _snapshot,
)


def _proposal(identity: str = "bundle-exhaustion:semantic") -> BundleExhaustionProposal:
    return BundleExhaustionProposal(
        proposal_identity=identity,
        stage_run_request_ref="stage-request:1",
        stage_run_request_receipt_ref="advancement_engine:receipt",
        stage_run_request_receipt_hash="b" * 64,
        cycle_ref="cycle:1",
        epoch=3,
        run_ref="bundle-run:1",
        attempt_ref="bundle-attempt:1",
        root_session_ref="bundle-session:1",
        execution_fence_ref="bundle-fence:1",
        context_pack_ref="context-pack:1",
        context_pack_hash="d" * 64,
        formal_plan_ref="formal-plan:1",
        formal_plan_content_hash="e" * 64,
        formal_plan_content_receipt=AcceptanceReceipt(
            issuer="research_graph",
            kind="formal_plan_content_accepted",
            receipt_ref="formal-plan-content:receipt",
            subject_ref="e" * 64,
            payload_hash="f" * 64,
        ),
        evidence_ref="bundle-exhaustion-evidence:semantic",
        evidence_hash="2" * 64,
        evidence_receipt=AcceptanceReceipt(
            issuer="agent_runtime",
            kind=BUNDLE_EXHAUSTION_EVIDENCE_RECEIPT_KIND,
            receipt_ref="bundle-exhaustion-evidence-receipt:semantic",
            subject_ref="bundle-exhaustion-evidence:semantic",
            payload_hash="3" * 64,
        ),
    )


class _ExhaustionAdvancement(_FakeAdvancementEngine):
    def __init__(self) -> None:
        super().__init__()
        self.submit_calls: list[dict[str, object]] = []
        self.accepted: BundleExhaustionOperationResult | None = None

    def submit_bundle_exhaustion_proposal(self, **values):
        self.submit_calls.append(values)
        proposal = values["proposal"]
        proposal_ref = "bundle-exhaustion-proposal:1"
        self.accepted = BundleExhaustionOperationResult(
            operation_ref="bundle-exhaustion-operation:1",
            proposal_identity=proposal.proposal_identity,
            proposal_hash=proposal.proposal_hash,
            status="accepted",
            accepted_proposal_ref=proposal_ref,
            decision_receipt=AcceptanceReceipt(
                issuer="advancement_engine",
                kind="bundle_exhaustion_proposal_accepted",
                receipt_ref="bundle-exhaustion-decision:1",
                subject_ref=proposal_ref,
                payload_hash="1" * 64,
            ),
        )
        return self.accepted

    def reconcile_bundle_exhaustion_proposal(self, **values):
        if self.accepted is None:
            return None
        assert values == {
            "proposal_identity": self.accepted.proposal_identity,
            "expected_proposal_hash": self.accepted.proposal_hash,
        }
        return self.accepted


def _gateway():
    advancement = _ExhaustionAdvancement()
    graph = _FakeResearchGraph()
    memory = _FakeResearchMemory()
    runtime = _FakeAgentRuntime()
    gateway = create_semantic_owner_gateway(
        research_graph=graph,
        advancement_engine=advancement,
        research_memory=memory,
        agent_runtime=runtime,
        human_collaboration_snapshot=lambda: _snapshot("human_collaboration"),
    )
    return gateway, advancement, runtime


def test_semantic_submit_and_lost_response_reconcile_exact_identity_hash() -> None:
    gateway, advancement, runtime = _gateway()
    operation_ids = (
        "advancement_engine.bundle_exhaustion.submit",
        "advancement_engine.bundle_exhaustion.reconcile",
    )
    channel = _issue(gateway, operation_ids)
    proposal = _proposal()
    arguments = {
        "effect_id": proposal.proposal_identity,
        "proposal": proposal.as_dict(),
    }

    unknown = _call(
        gateway,
        channel,
        "advancement_engine.bundle_exhaustion.reconcile",
        arguments,
    )
    assert unknown["isError"] is False
    assert unknown["structuredContent"] == {
        "status": "outcome_unknown",
        "effect_id": proposal.proposal_identity,
        "proposal_identity": proposal.proposal_identity,
        "proposal_hash": proposal.proposal_hash,
    }

    submitted = _call(
        gateway,
        channel,
        "advancement_engine.bundle_exhaustion.submit",
        arguments,
    )
    assert submitted["isError"] is False
    assert submitted["structuredContent"]["status"] == "accepted"
    assert submitted["structuredContent"]["proposal_hash"] == proposal.proposal_hash
    assert advancement.submit_calls[0]["proposal"] == proposal
    assert str(advancement.submit_calls[0]["idempotency_key"]).startswith(
        "mcp-effect:"
    )

    replay = _call(
        gateway,
        channel,
        "advancement_engine.bundle_exhaustion.reconcile",
        arguments,
    )
    assert replay["structuredContent"] == submitted["structuredContent"]
    assert runtime.scope_checks == 3


def test_semantic_scope_and_closed_schema_fail_before_ae_side_effect() -> None:
    gateway, advancement, runtime = _gateway()
    channel = _issue(
        gateway,
        (
            "advancement_engine.bundle_exhaustion.submit",
            "advancement_engine.bundle_exhaustion.reconcile",
        ),
    )
    proposal = _proposal("bundle-exhaustion:stale")
    arguments = {
        "effect_id": proposal.proposal_identity,
        "proposal": proposal.as_dict(),
    }
    runtime.stale = True
    stale = _call(
        gateway,
        channel,
        "advancement_engine.bundle_exhaustion.submit",
        arguments,
    )
    assert stale["isError"] is True
    assert stale["structuredContent"]["code"] == "semantic_call_scope_stale"
    assert advancement.submit_calls == []

    runtime.stale = False
    arguments["proposal"] = {**proposal.as_dict(), "attempt_budget": 3}
    extra = _call(
        gateway,
        channel,
        "advancement_engine.bundle_exhaustion.submit",
        arguments,
    )
    assert extra["isError"] is True
    assert advancement.submit_calls == []
