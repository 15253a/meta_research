"""Current Acquisition entries leave historical system help records inert."""
import json
from collections import defaultdict

from sqlalchemy import event
from sqlalchemy.engine import Engine

from meta_research.acquisition import AcquisitionBatchRequest, AcquisitionPaper, validate_batch_request
from meta_research.composition import build_production_runtime
from meta_research.owners.common import canonical_hash
from meta_research.paths import prepare_data_root
from test_public_acquisition_session import (
    NoCompute, RecordingAcquisitionProvider, _authenticated_client, _open_ready_acquisition,
)


def _historical_satisfied_help(runtime, session, request, waiting):
    """Create a verified old Owner record, without opening a current root effect."""
    owner = runtime.owners.agent_runtime
    target = {"schema_ref": "meta-research/acquisition-human-request-target/v1",
              "operation": "resume_acquisition_item", "session_ref": session.session_ref,
              "acquisition_request_id": request.request_id,
              "acquisition_request_hash": validate_batch_request(request),
              "acquisition_paper_id": "paper:waiting",
              "acquisition_item_hash": canonical_hash(waiting.results[0].as_dict()),
              "config_hash": session.config_hash,
              "blocked_preflight_generation": session.preflight_generation, "attempt_count": 1}
    waiter = {"waiter_ref": "acquisition_item:" + canonical_hash({"request_id": request.request_id, "paper_id": "paper:waiting"})[:32],
              "generation": 1, "target_assertion": target, "wait_scope": "local", "other_blockers": []}
    opened = owner.open_human_request(request_kind="library_reconnect",
        obligation="Restore access or provide an accepted lawful copy for the exact literature item paper:waiting.",
        business_purpose="Resume only the exact blocked literature item without repeating already obtained material.",
        target_assertion=target,
        acceptance_conditions=("A newer preflight verifies the exact session and config, or the Owner accepts an OA-only route for this exact item.",
                               "Any provided material is an accepted Research Asset bound to this exact acquisition item."),
        direct_waiter=waiter, idempotency_key="archived-acquisition-help")
    response = runtime.owners.human_collaboration.respond_to_human_request(opened["request_ref"],
        decision="provided", facts={"route": "oa_only"}, note="Use the historical narrower route.",
        idempotency_key="archived-acquisition-response")
    current = owner.query_human_request(opened["request_ref"])
    if current["status"] == "open":
        owner.evaluate_human_request(opened["request_ref"], response_refs=(response["response_ref"],),
            decision="satisfied", reason_code="oa_only_route_selected", accepted_evidence_refs=(),
            idempotency_key="archived-acquisition-evaluation")
        owner.validate_human_request_waiter(opened["request_ref"], waiter_ref=waiter["waiter_ref"],
            generation=1, target_assertion=target, other_blockers=(), idempotency_key="archived-acquisition-validation")
    historical = owner.query_human_request(opened["request_ref"])
    assert historical["status"] == "satisfied"
    assert historical["direct_waiters"][0]["status"] == "released"
    return historical


def test_restart_prepare_and_wait_retry_ignore_historical_help_and_routes(tmp_path):
    provider = RecordingAcquisitionProvider()
    data = prepare_data_root(tmp_path / "acquisition-legacy-retirement")
    runtime = build_production_runtime(data, acquisition_provider=provider, host_compute_probe=NoCompute())
    client, headers = _authenticated_client(runtime)
    initialization_id, saved, session_ref = _open_ready_acquisition(client, headers, prefix="legacy-retirement")
    client.close()
    request = AcquisitionBatchRequest(request_id="legacy-retirement-batch", route_policy="oa_first_then_institution",
        papers=(AcquisitionPaper(paper_id="paper:waiting", title="Current exact request", doi=None, arxiv_id=None, source_urls=()),))
    waiting = runtime.owners.agent_runtime.acquire_literature(session_ref, request, provider)
    session = runtime.owners.agent_runtime.query_acquisition_session(session_ref=session_ref)
    historical = _historical_satisfied_help(runtime, session, request, waiting)
    runtime.close()
    counts = defaultdict(lambda: {"old_request_scans": 0, "old_route_reads": 0, "old_route_writes": 0,
                                  "old_waiter_consumptions": 0, "old_startup_wait_scans": 0})
    phase = "restart"
    def observe(_connection, _cursor, statement, _parameters, _context, _many):
        counts[phase]
        sql = " ".join(statement.lower().split())
        if "from owner_human_requests" in sql and "kind = 'library_reconnect'" in sql:
            counts[phase]["old_request_scans"] += 1
        if "from ar_acquisition_resume_routes" in sql:
            counts[phase]["old_route_reads"] += 1
        if "insert into ar_acquisition_resume_routes" in sql:
            counts[phase]["old_route_writes"] += 1
        if "insert into owner_human_request_resume_consumptions" in sql:
            counts[phase]["old_waiter_consumptions"] += 1
        if "select distinct sessions.session_ref from ar_acquisition_requests" in sql:
            counts[phase]["old_startup_wait_scans"] += 1
    event.listen(Engine, "before_cursor_execute", observe)
    try:
        runtime = build_production_runtime(data, acquisition_provider=provider, host_compute_probe=NoCompute())
        phase = "waiting_retry"
        statuses = [runtime.owners.agent_runtime.acquire_literature(session_ref, request, provider).status for _ in range(3)]
        calls_before_preflight = len(provider.batches)
        phase = "preflight"
        config = {key: saved["quest_draft"]["value"]["literature"][key] for key in ("mode", "library_entry_url")}
        for _ in range(2):
            runtime.owners.agent_runtime.prepare_acquisition_session(initialization_id=initialization_id,
                draft_revision=saved["quest_draft"]["revision"], config=config, provider=provider)
        phase = "ready_retry"
        result = runtime.owners.agent_runtime.acquire_literature(session_ref, request, provider)
        after = runtime.owners.agent_runtime.query_human_request(historical["request_ref"])
        print(json.dumps({"counts": dict(counts), "before_preflight": statuses,
                          "calls_before_preflight": calls_before_preflight, "provider_calls": len(provider.batches)}, sort_keys=True))
        assert statuses == ["waiting_user"] * 3 and calls_before_preflight == 1
        assert result.status == "missing" and len(provider.batches) == 2
        assert after == historical
        assert all(value == 0 for phase_counts in counts.values() for value in phase_counts.values())
    finally:
        event.remove(Engine, "before_cursor_execute", observe)
        runtime.close()
