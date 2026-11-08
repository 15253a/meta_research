"""Resident host consumers retain diagnostics from real scoped Owner calls."""
from types import SimpleNamespace

import pytest

from meta_research.research_content import research_content_operations
from meta_research.root_resident_mcp import RootResidentMcpChannels, RootResidentMcpError
from meta_research.semantic_mcp import SemanticMcpGateway
from test_public_bundle_stage import _bundle_runtime, _prepare_bundle_request
from test_research_datasets import _asset
from test_tool_contract_alignment import _channel, _call


def _resident(monkeypatch, gateway, channel, scope, operation):
    channels = RootResidentMcpChannels("bundle")
    channels.bind_authority(SimpleNamespace(dispatch_mcp=gateway.dispatch))
    # Acquisition/lifetime are outside this adapter seam; the bearer and Owner are real.
    monkeypatch.setattr(channels, "acquire", lambda **_: ("issued", channel))
    monkeypatch.setattr(channels, "release", lambda _: None)
    credentials = {key: scope[key] for key in (
        "run_ref", "attempt_ref", "root_session_ref", "fence_ref")}
    credentials.update(capability_binding_hash=scope["runtime_binding_hash"],
        phase="primary", job_ref=None, operation_id=operation)
    return channels, credentials


def test_resident_preserves_owner_rejection_and_continues_original_scope(monkeypatch, tmp_path):
    runtime = _bundle_runtime(tmp_path / "resident")
    try:
        _prepare_bundle_request(runtime)
        current = runtime.bundle_stage.query_current()
        request = runtime.owners.advancement_engine.query_bundle_stage_request(
            current["stage_run_request"]["cycle_ref"])
        assert runtime.bundle_stage.process_once()
        run = runtime.owners.agent_runtime.query_bundle_stage_run(request.request_ref)
        quest_ref = request.accepted_question.quest_ref
        graph, memory = runtime.owners.research_graph, runtime.owners.research_memory
        binding = _asset(runtime)
        graph.accept_asset_role(binding=binding, role="evidence", quest_ref=quest_ref,
            idempotency_key="protected-origin")
        scope = {key: getattr(run, key) for key in (
            "run_ref", "attempt_ref", "root_session_ref", "fence_ref", "runtime_binding_hash")}
        gateway = SemanticMcpGateway(research_content_operations(
            research_graph=graph, research_memory=memory,
            agent_runtime=runtime.owners.agent_runtime))
        channel = _channel(gateway, scope, root_kind="bundle")
        channels, credentials = _resident(monkeypatch, gateway, channel, scope,
            "research_memory.assets.retire")
        lifecycle = _call(gateway, channel, "research_memory.assets.lifecycle",
            {"version_ref": binding.version_ref})["structuredContent"]
        arguments = {"version_ref": binding.version_ref, "expected_revision": lifecycle["revision"],
            "expected_reference_revision": lifecycle["reference_revision"],
            "explanation": "Retain research while testing diagnostics.",
            "low_value": True, "obsolete": True, "incorrect": True,
            "impact_understood": True, "has_explanation_value": False,
            "effect_id": "original-retirement-effect"}
        with pytest.raises(RootResidentMcpError) as caught:
            channels.call_operation(**credentials, arguments=arguments)
        assert caught.value.code == "asset_retirement_blocked"
        assert caught.value.details["reasons"] == ["active_references"]
        assert caught.value.details["active_reference_refs"]
        assert caught.value.details["observed_reference_revision"] == lifecycle["reference_revision"]
        credentials["operation_id"] = "research_memory.assets.retire.reconcile"
        assert channels.call_operation(**credentials, arguments=arguments) == {
            "status": "not_found", "result": None}
        credentials["operation_id"] = "research_memory.content.read"
        assert channels.call_operation(**credentials, arguments={
            "source_ref": binding.version_ref, "version_ref": binding.version_ref})["text"]
    finally:
        runtime.close()



@pytest.mark.parametrize(("status", "payload", "code", "category"), [
    (401, {"error": {"code": "mcp_channel_authentication_required", "message": "secret-token"}},
     "mcp_channel_authentication_required", "authorization"),
    (200, {"jsonrpc": "2.0", "id": 1, "error": {"code": -32602, "message": "secret-token"}},
     "semantic_mcp_protocol_error", "host_contract"),
    (503, None, "semantic_mcp_unavailable", "temporarily_unavailable"),
    (200, {"result": {}}, "semantic_mcp_response_invalid", "host_contract"),
])
def test_resident_distinguishes_protocol_auth_and_unavailable(monkeypatch, status, payload, code, category):
    # Controlled transport faults, not a claim of a real provider failure.
    channels = RootResidentMcpChannels("deepfetch")
    channels.bind_authority(SimpleNamespace(dispatch_mcp=lambda *_: (status, payload)))
    monkeypatch.setattr(channels, "acquire", lambda **_: ("issued", SimpleNamespace(token="bearer")))
    monkeypatch.setattr(channels, "release", lambda _: None)
    with pytest.raises(RootResidentMcpError) as caught:
        channels.call_operation(run_ref="run", attempt_ref="attempt", root_session_ref="root",
            fence_ref="fence", capability_binding_hash="a" * 64, phase="primary", job_ref=None,
            operation_id="agent_runtime.acquisition.request", arguments={"effect_id": "original-effect"})
    assert caught.value.code == code
    assert caught.value.details["category"] == category
    assert caught.value.details["correction"]
    assert "secret-token" not in str(caught.value.details)
    if category == "host_contract":
        assert caught.value.details["operation_id"] == "agent_runtime.acquisition.request"
    if code == "semantic_mcp_protocol_error":
        assert caught.value.details["protocol_code"] == -32602


def test_bundle_conflict_preserves_reason_then_reconciles_original_proposal(tmp_path):
    from dataclasses import replace
    from meta_research.bundle_exhaustion import BundleExhaustionProposal
    from meta_research.semantic_owner_gateway import create_semantic_owner_gateway
    from test_bundle_exhaustion_owner import _ExhaustionBundleSkill, _advance_to_evidence

    runtime = _bundle_runtime(tmp_path / "exhaustion", bundle_skill_provider=_ExhaustionBundleSkill())
    try:
        request, run, evidence = _advance_to_evidence(runtime)
        graph = runtime.owners.research_graph
        accepted = request.accepted_formal_plan
        formal = graph.accept_formal_plan_content(formal_plan_ref=accepted.formal_plan_ref,
            idempotency_key="diagnostic-formal-content")
        proposal = BundleExhaustionProposal(
            proposal_identity="diagnostic-proposal", stage_run_request_ref=request.request_ref,
            stage_run_request_receipt_ref=request.receipt.receipt_ref,
            stage_run_request_receipt_hash=request.receipt.payload_hash,
            cycle_ref=request.cycle_ref, epoch=request.epoch, run_ref=run.run_ref,
            attempt_ref=run.attempt_ref, root_session_ref=run.root_session_ref,
            execution_fence_ref=run.fence_ref, context_pack_ref=request.context_pack_ref,
            context_pack_hash=request.context_pack_hash, formal_plan_ref=formal.formal_plan_ref,
            formal_plan_content_hash=formal.plan_document_hash, formal_plan_content_receipt=formal.receipt,
            evidence_ref=evidence.evidence_ref, evidence_hash=evidence.evidence.evidence_hash,
            evidence_receipt=evidence.receipt)
        gateway = create_semantic_owner_gateway(research_graph=graph,
            advancement_engine=runtime.owners.advancement_engine,
            research_memory=runtime.owners.research_memory, agent_runtime=runtime.owners.agent_runtime,
            human_collaboration_snapshot=runtime.owners.human_collaboration.query_snapshot)
        scope = {key: getattr(run, key) for key in (
            "run_ref", "attempt_ref", "root_session_ref", "fence_ref", "runtime_binding_hash")}
        operations = ("advancement_engine.bundle_exhaustion.submit", "advancement_engine.bundle_exhaustion.reconcile")
        channel = _channel(gateway, scope, root_kind="bundle", operation_ids=operations)
        original = {"effect_id": proposal.proposal_identity, "proposal": proposal.as_dict()}
        submitted = _call(gateway, channel, operations[0], original)
        assert submitted["isError"] is False, submitted
        assert submitted["structuredContent"]["status"] == "accepted", submitted
        changed = {**original, "proposal": replace(proposal, evidence_hash="f" * 64).as_dict()}
        for operation in operations:
            rejected = _call(gateway, channel, operation, changed)
            assert rejected["isError"] is True
            assert rejected["structuredContent"]["code"] == "bundle_exhaustion_proposal_conflict"
            assert "original" in rejected["structuredContent"]["details"]["correction"].lower()
        recovered = _call(gateway, channel, operations[1], original)
        assert recovered["structuredContent"] == submitted["structuredContent"]
    finally:
        runtime.close()


def test_deepfetch_host_failure_diagnostics_survive_worker_and_restart(tmp_path):
    from meta_research.composition import build_production_runtime
    from meta_research.deepfetch import DeepFetchUnavailable
    from meta_research.paths import prepare_data_root
    from test_public_first_question_deepfetch import (
        DeterministicDeepFetchProvider, DeterministicProbe, RecordingAcquisitionProvider,
        _authenticate, _open_and_queue_deepfetch)
    from test_public_manual_question_lifecycle import DeterministicDraftingAdapter

    diagnostic = {"category": "host_contract", "message": "The host returned an invalid acquisition receipt.",
        "correction": "Repair the host response contract, then reconcile the original effect before resubmission.",
        "operation_id": "agent_runtime.acquisition.request", "effect_id": "original-acquisition",
        "effect_state": "unknown"}

    class HostReceiptFailure(DeterministicDeepFetchProvider):
        requires_verified_terminal_retry = True

        def execute(self, request):
            self.requests.append(request)
            raise DeepFetchUnavailable("semantic_mcp_response_invalid", details=diagnostic,
                native_session_ref="native-diagnostic-root")

    data_root = prepare_data_root(tmp_path / "durable-diagnostic")
    provider = HostReceiptFailure()

    def build():
        drafter = DeterministicDraftingAdapter()
        return build_production_runtime(data_root, proposal_drafter=drafter,
            intent_drafting_provider=drafter, deepfetch_provider=provider,
            host_compute_probe=DeterministicProbe(), acquisition_provider=RecordingAcquisitionProvider())

    runtime = build()
    client, headers = _authenticate(runtime)
    try:
        initialization, queued = _open_and_queue_deepfetch(client, headers, key_prefix="diagnostics")
        assert runtime.deepfetch.process_once()
        assert len(provider.requests) == 1
        failed = client.get(f"/api/v1/quest-initializations/{initialization}").json()
        run = failed["deepfetch"]["run"]
        assert run["failure"]["details"] == diagnostic
        assert failed["deepfetch"]["failure"] == run["failure"]
        assert run["failure"]["code"] == "semantic_mcp_response_invalid"
        assert run["provider_operation_retry_permitted"] is False
        assert run["native_session_ref"] == "native-diagnostic-root"
        assert not runtime.deepfetch.process_once()
    finally:
        client.close()
        runtime.close()
    restarted = build()
    client, _ = _authenticate(restarted)
    try:
        observed = client.get(f"/api/v1/quest-initializations/{initialization}").json()["deepfetch"]["run"]
        assert observed["run_ref"] == run["run_ref"]
        assert observed["attempt_ref"] == run["attempt_ref"]
        assert observed["failure"] == run["failure"]
        assert observed["provider_operation_retry_permitted"] is False
        assert not restarted.deepfetch.process_once()
        assert len(provider.requests) == 1
    finally:
        client.close()
        restarted.close()
