from __future__ import annotations

import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from meta_research.composition import build_production_runtime
from meta_research.owners.common import OwnerConflict
from meta_research.owners import agent_runtime as agent_runtime_module
from meta_research.owners import human_requests as human_requests_module
from meta_research.paths import prepare_data_root
from meta_research.quest_drafting import (
    IntentTurnResult,
    ProposalDraftResult,
)
from meta_research.semantic_mcp import ROOT_AGENT_HUMAN_REQUEST_OPERATION_IDS
from meta_research.owners.research_memory import AssetIntakeRequest
from meta_research.web import create_app


class _DeterministicDraftingProvider:
    def draft(self, _request) -> ProposalDraftResult:
        return ProposalDraftResult(
            content={
                "title": "A bounded question",
                "unknown_statement": "What remains unknown?",
                "answer_shape": "A falsifiable answer",
                "applicability_scope": "This Quest",
                "background_context": "",
                "requirements_constraints": "",
            },
            adapter_kind="deterministic_test_adapter",
        )

    def reply(self, request) -> IntentTurnResult:
        return IntentTurnResult(
            reply=f"Acknowledged: {request.message}",
            native_session_ref=request.native_session_ref or "native_test_session",
            adapter_kind="deterministic_test_adapter",
        )


def _waiter(ref: str, *, blocker: str | None = None) -> dict[str, object]:
    return {
        "waiter_ref": ref,
        "generation": 3,
        "target_assertion": {
            "session_ref": "acquisition_session_1",
            "preflight_generation": 3,
        },
        "wait_scope": "local",
        "other_blockers": [] if blocker is None else [blocker],
    }


def _open_library_request(owner, waiter_ref: str, key: str) -> dict[str, object]:
    return owner.open_human_request(
        request_kind="library_reconnect",
        obligation="Reconnect the institution-backed browser profile.",
        business_purpose="Resume the exact literature acquisition session.",
        target_assertion={
            "session_ref": "acquisition_session_1",
            "preflight_generation": 3,
        },
        acceptance_conditions=(
            "The acquisition preflight can use the institution route.",
        ),
        direct_waiter=_waiter(waiter_ref),
        idempotency_key=key,
    )


def _satisfy_library_request(runtime, request_ref: str, key: str) -> None:
    response = runtime.owners.human_collaboration.respond_to_human_request(
        request_ref,
        decision="provided",
        facts={"preflight_ref": f"preflight-{key}", "route_status": "ready"},
        note="The exact acquisition preflight reports ready.",
        idempotency_key=f"{key}-response",
    )
    satisfied = runtime.owners.agent_runtime.evaluate_human_request(
        request_ref,
        response_refs=(response["response_ref"],),
        decision="satisfied",
        reason_code="institution_route_verified",
        accepted_evidence_refs=(f"preflight-{key}",),
        idempotency_key=f"{key}-evaluation",
    )
    assert satisfied["disposition"]["decision"] == "satisfied"


def test_request_and_response_business_text_round_trip_without_content_filtering(
    tmp_path,
) -> None:
    provider = _DeterministicDraftingProvider()
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "human-request-raw-business-text"),
        proposal_drafter=provider,
        intent_drafting_provider=provider,
    )
    owner = runtime.owners.research_graph
    target_assertion = {
        "operation": "resume_external_material_fetch",
        "api_token": "ghp_test_only_credential",
    }
    acceptance_conditions = (
        "Use Cookie sessionid=test-only-cookie for the exact external request.",
    )
    required_authorization = {
        "capability": "external_material_api_access",
        "password": "test-only-password",
    }
    try:
        opened = owner.open_human_request(
            request_kind="external_material_api_access",
            obligation="  Provide API key sk-test-not-a-real-secret.\n",
            business_purpose=(
                "\tResume the request with password=test-only-password.  "
            ),
            target_assertion=target_assertion,
            acceptance_conditions=acceptance_conditions,
            required_authorization=required_authorization,
            direct_waiter={
                "waiter_ref": "external_material_fetch_1",
                "generation": 1,
                "target_assertion": target_assertion,
                "wait_scope": "local",
                "other_blockers": [],
            },
            idempotency_key="open-raw-business-text",
        )

        assert opened["obligation"] == (
            "  Provide API key sk-test-not-a-real-secret.\n"
        )
        assert opened["business_purpose"] == (
            "\tResume the request with password=test-only-password.  "
        )
        assert opened["target_assertion"] == target_assertion
        assert opened["acceptance_conditions"] == list(acceptance_conditions)
        assert opened["required_authorization"] == required_authorization

        response = runtime.owners.human_collaboration.respond_to_human_request(
            opened["request_ref"],
            decision="provided",
            facts={"api_key": "sk-test-response-only"},
            note="\nUse password=test-only-response-password.\t",
            idempotency_key="respond-raw-business-text",
        )
        assert response["facts"] == {"api_key": "sk-test-response-only"}
        assert response["note"] == (
            "\nUse password=test-only-response-password.\t"
        )

        evaluated = owner.evaluate_human_request(
            opened["request_ref"],
            response_refs=(response["response_ref"],),
            decision="satisfied",
            reason_code="human_response_accepted",
            accepted_evidence_refs=(),
            idempotency_key="evaluate-raw-business-text",
        )
        assert evaluated["responses"] == [response]
        assert evaluated["response_rejections"] == []
        assert evaluated["disposition"]["decision"] == "satisfied"

        failed_operation = {"operation_ref": "failed_operation_1"}
        system_help = owner.open_human_request(
            request_kind="system_operation_help",
            obligation="Retry the exact failed operation.",
            business_purpose="Restore only its direct dependency.",
            target_assertion=failed_operation,
            acceptance_conditions=("The bound operation is retried.",),
            direct_waiter={
                "waiter_ref": "failed_operation_1",
                "generation": 1,
                "target_assertion": failed_operation,
                "wait_scope": "local",
                "other_blockers": [],
            },
            idempotency_key="open-system-operation-help",
        )
        assert system_help["kind"] == "system_operation_help"
        assert owner.query_human_request(system_help["request_ref"]) == system_help
    finally:
        runtime.close()


def test_response_evaluation_disposition_and_waiter_resume_are_distinct(tmp_path) -> None:
    provider = _DeterministicDraftingProvider()
    data_root = prepare_data_root(tmp_path / "human-requests")
    runtime = build_production_runtime(
        data_root,
        proposal_drafter=provider,
        intent_drafting_provider=provider,
    )
    try:
        first = _open_library_request(
            runtime.owners.agent_runtime, "deepfetch_run_1", "open-library-1"
        )
        reused = _open_library_request(
            runtime.owners.agent_runtime, "deepfetch_run_2", "open-library-2"
        )
        assert reused["request_ref"] == first["request_ref"]
        assert reused["revision"] == 1
        assert [item["waiter_ref"] for item in reused["direct_waiters"]] == [
            "deepfetch_run_1",
            "deepfetch_run_2",
        ]

        # Identity is issuer-owned. An otherwise exact request from another
        # Owner must never be shared with Agent Runtime.
        cross_owner = _open_library_request(
            runtime.owners.research_memory, "asset_intake_1", "open-library-rm"
        )
        assert cross_owner["request_id"] != first["request_id"]
        assert cross_owner["issuer"] == "research_memory"

        partial = runtime.owners.human_collaboration.respond_to_human_request(
            first["request_ref"],
            decision="provided",
            facts={"route": "institutional_browser", "profile_ref": "opaque:lab"},
            note="The profile was reconnected; verify the exact route.",
            idempotency_key="respond-library-partial",
        )
        observed = runtime.owners.agent_runtime.query_human_request(
            first["request_ref"]
        )
        assert observed is not None
        assert observed["responses"] == [partial]
        assert observed["evaluation"] is None
        assert observed["disposition"] is None
        assert all(item["status"] == "blocked" for item in observed["direct_waiters"])

        needs_input = runtime.owners.agent_runtime.evaluate_human_request(
            first["request_ref"],
            response_refs=(partial["response_ref"],),
            decision="needs_input",
            reason_code="institution_route_not_yet_verified",
            accepted_evidence_refs=(),
            idempotency_key="evaluate-library-partial",
        )
        assert needs_input["evaluation"]["decision"] == "needs_input"
        assert needs_input["disposition"] is None

        supplement = runtime.owners.human_collaboration.respond_to_human_request(
            first["request_ref"],
            decision="provided",
            facts={"preflight_ref": "preflight_3", "route_status": "ready"},
            note="The exact acquisition preflight now reports ready.",
            idempotency_key="respond-library-supplement",
        )
        satisfied = runtime.owners.agent_runtime.evaluate_human_request(
            first["request_ref"],
            response_refs=(partial["response_ref"], supplement["response_ref"]),
            decision="satisfied",
            reason_code="institution_route_verified",
            accepted_evidence_refs=("preflight_3",),
            idempotency_key="evaluate-library-satisfied",
        )
        assert satisfied["evaluation"]["decision"] == "satisfied"
        assert satisfied["disposition"]["decision"] == "satisfied"
        assert all(item["status"] == "blocked" for item in satisfied["direct_waiters"])

        blocked = runtime.owners.agent_runtime.validate_human_request_waiter(
            first["request_ref"],
            waiter_ref="deepfetch_run_2",
            generation=3,
            target_assertion=_waiter("unused")["target_assertion"],
            other_blockers=("provider_unavailable",),
            idempotency_key="resume-library-2-blocked",
        )
        assert blocked["status"] == "blocked"
        assert blocked["reason"]["code"] == "other_blockers_present"

        released = runtime.owners.agent_runtime.validate_human_request_waiter(
            first["request_ref"],
            waiter_ref="deepfetch_run_1",
            generation=3,
            target_assertion=_waiter("unused")["target_assertion"],
            other_blockers=(),
            idempotency_key="resume-library-1",
        )
        assert released["status"] == "released"
        assert released["started_work"] is False

        duplicate_release = (
            runtime.owners.agent_runtime.validate_human_request_waiter(
                first["request_ref"],
                waiter_ref="deepfetch_run_1",
                generation=3,
                target_assertion=_waiter("unused")["target_assertion"],
                other_blockers=(),
                idempotency_key="resume-library-1-different-command",
            )
        )
        assert duplicate_release["status"] == "blocked"
        assert duplicate_release["reason"] == {
            "code": "waiter_not_resumable"
        }
    finally:
        runtime.close()

    restarted = build_production_runtime(
        data_root,
        proposal_drafter=provider,
        intent_drafting_provider=provider,
    )
    try:
        replayed_response = (
            restarted.owners.human_collaboration.respond_to_human_request(
                first["request_ref"],
                decision="provided",
                facts={
                    "route": "institutional_browser",
                    "profile_ref": "opaque:lab",
                },
                note="The profile was reconnected; verify the exact route.",
                idempotency_key="respond-library-partial",
            )
        )
        assert replayed_response["response_ref"] == partial["response_ref"]
        assert replayed_response["receipt_ref"] == partial["receipt_ref"]
        recovered = restarted.owners.agent_runtime.query_human_request(
            first["request_ref"]
        )
        assert recovered is not None
        assert recovered["disposition"]["decision"] == "satisfied"
        waiter_states = {
            item["waiter_ref"]: item["status"]
            for item in recovered["direct_waiters"]
        }
        assert waiter_states == {
            "deepfetch_run_1": "released",
            "deepfetch_run_2": "blocked",
        }
        replay = restarted.owners.agent_runtime.validate_human_request_waiter(
            first["request_ref"],
            waiter_ref="deepfetch_run_1",
            generation=3,
            target_assertion=_waiter("unused")["target_assertion"],
            other_blockers=(),
            idempotency_key="resume-library-1",
        )
        assert replay["validation_ref"] == released["validation_ref"]
    finally:
        restarted.close()


def test_human_response_cannot_commit_after_exact_request_becomes_stale(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _DeterministicDraftingProvider()
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "human-response-currentness-race"),
        proposal_drafter=provider,
        intent_drafting_provider=provider,
    )
    owner = runtime.owners.agent_runtime
    human = runtime.owners.human_collaboration
    try:
        request = _open_library_request(
            owner,
            "deepfetch_response_currentness_race",
            "open-response-currentness-race",
        )
        original_query = human._query_issuing_owner_request
        raced = False

        def query_then_supersede(request_ref: str) -> dict[str, object]:
            nonlocal raced
            observed = original_query(request_ref)
            if not raced:
                raced = True
                owner.revise_human_request(
                    request_ref,
                    expected_revision=request["revision"],
                    obligation=(
                        "Reconnect the exact institution-backed browser profile "
                        "after its route changed."
                    ),
                    target_assertion={
                        "session_ref": "acquisition_session_1",
                        "preflight_generation": 4,
                    },
                    acceptance_conditions=(
                        "The revised acquisition preflight can use the institution route.",
                    ),
                    direct_waiters=(
                        {
                            **_waiter("deepfetch_response_currentness_race"),
                            "generation": 4,
                            "target_assertion": {
                                "session_ref": "acquisition_session_1",
                                "preflight_generation": 4,
                            },
                        },
                    ),
                    idempotency_key="revise-during-response-currentness-check",
                )
            return observed

        monkeypatch.setattr(
            human,
            "_query_issuing_owner_request",
            query_then_supersede,
        )
        with pytest.raises(OwnerConflict, match="human_request_not_current"):
            human.respond_to_human_request(
                request["request_ref"],
                decision="provided",
                facts={"route_status": "ready"},
                note="This response raced with an Owner revision.",
                idempotency_key="response-currentness-race",
            )
        assert human._fact_verifier.query_human_responses(request["request_ref"]) == ()
    finally:
        runtime.close()


def test_expired_request_is_durably_terminal_and_cannot_release_work(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _DeterministicDraftingProvider()
    data_root = prepare_data_root(tmp_path / "human-request-expiry")
    runtime = build_production_runtime(
        data_root,
        proposal_drafter=provider,
        intent_drafting_provider=provider,
    )
    owner = runtime.owners.agent_runtime
    now = time.time()
    try:
        with pytest.raises(OwnerConflict, match="human_request_expiry_invalid"):
            owner.open_human_request(
                request_kind="library_reconnect",
                obligation="Reconnect an already expired route.",
                business_purpose="This request must never become actionable.",
                target_assertion={"session_ref": "expired_before_open"},
                acceptance_conditions=("The route is ready.",),
                direct_waiter=_waiter("expired_before_open"),
                expires_at=now - 1,
                idempotency_key="expired-before-open",
            )

        request = owner.open_human_request(
            request_kind="library_reconnect",
            obligation="Reconnect the route before this bounded deadline.",
            business_purpose="Resume only while the exact obligation is current.",
            target_assertion={"session_ref": "expires_after_open"},
            acceptance_conditions=("The route is ready before expiry.",),
            direct_waiter={
                **_waiter("expires_after_open"),
                "target_assertion": {"session_ref": "expires_after_open"},
            },
            expires_at=now + 10,
            idempotency_key="expires-after-open",
        )
        monkeypatch.setattr(human_requests_module.time, "time", lambda: now + 20)

        expired = owner.query_human_request(request["request_ref"])
        assert expired is not None
        assert expired["status"] == "expired"
        assert expired["current"] is True
        assert expired["disposition"]["decision"] == "expired"
        assert expired["direct_waiters"][0]["status"] == "cancelled"
        with pytest.raises(OwnerConflict, match="human_request_not_current"):
            runtime.owners.human_collaboration.respond_to_human_request(
                request["request_ref"],
                decision="provided",
                facts={"route_status": "ready"},
                note="This arrived after the deadline.",
                idempotency_key="response-after-expiry",
            )
    finally:
        runtime.close()

    restarted = build_production_runtime(
        data_root,
        proposal_drafter=provider,
        intent_drafting_provider=provider,
    )
    try:
        persisted = restarted.owners.agent_runtime.query_human_request(
            request["request_ref"]
        )
        assert persisted is not None
        assert persisted["status"] == "expired"
        assert persisted["disposition"]["receipt"] == expired["disposition"][
            "receipt"
        ]
    finally:
        restarted.close()


def test_owner_current_blocker_evaluation_can_clear_a_recovered_waiter(
    tmp_path,
) -> None:
    provider = _DeterministicDraftingProvider()
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "persisted-waiter-blocker"),
        proposal_drafter=provider,
        intent_drafting_provider=provider,
    )
    owner = runtime.owners.agent_runtime
    try:
        request = owner.open_human_request(
            request_kind="library_reconnect",
            obligation="Reconnect the institution-backed browser profile.",
            business_purpose="Resume the exact literature acquisition session.",
            target_assertion={
                "session_ref": "acquisition_session_1",
                "preflight_generation": 3,
            },
            acceptance_conditions=(
                "The acquisition preflight can use the institution route.",
            ),
            direct_waiter=_waiter(
                "deepfetch_persisted_blocker",
                blocker="provider_unavailable",
            ),
            idempotency_key="open-persisted-waiter-blocker",
        )
        _satisfy_library_request(
            runtime,
            request["request_ref"],
            "persisted-waiter-blocker",
        )

        validation = owner.validate_human_request_waiter(
            request["request_ref"],
            waiter_ref="deepfetch_persisted_blocker",
            generation=3,
            target_assertion=_waiter("unused")["target_assertion"],
            other_blockers=(),
            idempotency_key="resume-persisted-waiter-blocker",
        )
        assert validation["status"] == "released"
        assert validation["reason"] is None

        persisted = owner.query_human_request(request["request_ref"])
        assert persisted is not None
        persisted_waiter = persisted["direct_waiters"][0]
        assert persisted_waiter["waiter_ref"] == "deepfetch_persisted_blocker"
        assert persisted_waiter["generation"] == 3
        assert persisted_waiter["target_assertion"] == _waiter("unused")[
            "target_assertion"
        ]
        assert persisted_waiter["other_blockers"] == []
        assert persisted_waiter["status"] == "released"
        assert persisted_waiter["resume_validation"]["validation_ref"] == (
            validation["validation_ref"]
        )
    finally:
        runtime.close()


def test_waiter_validation_rejects_tampered_satisfied_disposition(
    tmp_path,
) -> None:
    provider = _DeterministicDraftingProvider()
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "tampered-satisfied-disposition"),
        proposal_drafter=provider,
        intent_drafting_provider=provider,
    )
    owner = runtime.owners.agent_runtime
    try:
        request = _open_library_request(
            owner,
            "deepfetch_tampered_disposition",
            "open-tampered-disposition",
        )
        _satisfy_library_request(
            runtime,
            request["request_ref"],
            "tampered-disposition",
        )

        with runtime._database.write() as connection:
            connection.execute(
                text(
                    "UPDATE owner_human_request_dispositions SET receipt_hash = "
                    ":receipt_hash WHERE request_ref = :request_ref"
                ),
                {
                    "receipt_hash": "f" * 64,
                    "request_ref": request["request_ref"],
                },
            )

        with pytest.raises(OwnerConflict, match="human_request_disposition_invalid"):
            owner.query_human_request(request["request_ref"])
        with pytest.raises(OwnerConflict, match="human_request_disposition_invalid"):
            owner.validate_human_request_waiter(
                request["request_ref"],
                waiter_ref="deepfetch_tampered_disposition",
                generation=3,
                target_assertion=_waiter("unused")["target_assertion"],
                other_blockers=(),
                idempotency_key="resume-tampered-disposition",
            )
    finally:
        runtime.close()


def test_query_rejects_disposition_bound_to_a_tampered_evaluation(
    tmp_path,
) -> None:
    provider = _DeterministicDraftingProvider()
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "tampered-human-request-evaluation"),
        proposal_drafter=provider,
        intent_drafting_provider=provider,
    )
    owner = runtime.owners.agent_runtime
    try:
        request = _open_library_request(
            owner,
            "deepfetch_tampered_evaluation",
            "open-tampered-evaluation",
        )
        _satisfy_library_request(
            runtime,
            request["request_ref"],
            "tampered-evaluation",
        )
        with runtime._database.write() as connection:
            connection.execute(
                text(
                    "UPDATE owner_human_request_evaluations SET decision = "
                    "'needs_input' WHERE request_ref = :request_ref"
                ),
                {"request_ref": request["request_ref"]},
            )

        with pytest.raises(OwnerConflict, match="human_request_disposition_invalid"):
            owner.query_human_request(request["request_ref"])
    finally:
        runtime.close()


def test_terminal_request_status_and_current_revision_are_receipt_consistent(
    tmp_path,
) -> None:
    provider = _DeterministicDraftingProvider()
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "human-request-state-integrity"),
        proposal_drafter=provider,
        intent_drafting_provider=provider,
    )
    owner = runtime.owners.agent_runtime
    try:
        request = _open_library_request(
            owner,
            "deepfetch_state_integrity",
            "open-state-integrity",
        )
        _satisfy_library_request(runtime, request["request_ref"], "state-integrity")
        with runtime._database.write() as connection:
            connection.execute(
                text(
                    "UPDATE owner_human_requests SET status = 'open' WHERE "
                    "request_ref = :request_ref"
                ),
                {"request_ref": request["request_ref"]},
            )

        with pytest.raises(OwnerConflict, match="human_request_artifact_invalid"):
            owner.query_human_request(request["request_ref"])
        with pytest.raises(OwnerConflict, match="human_request_artifact_invalid"):
            runtime.owners.human_collaboration.respond_to_human_request(
                request["request_ref"],
                decision="provided",
                facts={"route_status": "ready"},
                note="A terminal request cannot be reopened by mutable status.",
                idempotency_key="response-after-status-tamper",
            )
    finally:
        runtime.close()


def test_successor_terminal_recurrence_and_raw_response(tmp_path) -> None:
    provider = _DeterministicDraftingProvider()
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "human-request-revisions"),
        proposal_drafter=provider,
        intent_drafting_provider=provider,
    )
    try:
        original = runtime.owners.research_graph.open_human_request(
            request_kind="offline_action",
            obligation="Place the instrument in calibration mode.",
            business_purpose="Run the exact accepted protocol.",
            target_assertion={"target_ref": "target_1", "protocol_revision": 4},
            acceptance_conditions=("Calibration mode is visibly active.",),
            direct_waiter={
                "waiter_ref": "target_run_1",
                "generation": 1,
                "target_assertion": {
                    "target_ref": "target_1",
                    "protocol_revision": 4,
                },
                "wait_scope": "quest",
                "other_blockers": [],
            },
            idempotency_key="open-offline",
        )
        successor = runtime.owners.research_graph.revise_human_request(
            original["request_ref"],
            expected_revision=1,
            obligation="Place the instrument in calibration mode and record its display.",
            target_assertion={"target_ref": "target_1", "protocol_revision": 5},
            acceptance_conditions=(
                "Calibration mode is visibly active.",
                "A non-secret display receipt is attached.",
            ),
            direct_waiters=(
                {
                    "waiter_ref": "target_run_1",
                    "generation": 2,
                    "target_assertion": {
                        "target_ref": "target_1",
                        "protocol_revision": 5,
                    },
                    "wait_scope": "quest",
                    "other_blockers": [],
                },
            ),
            idempotency_key="revise-offline",
        )
        assert successor["request_id"] == original["request_id"]
        assert successor["revision"] == 2
        assert successor["request_ref"] != original["request_ref"]
        superseded = runtime.owners.research_graph.query_human_request(
            original["request_ref"]
        )
        assert superseded is not None
        assert superseded["disposition"]["decision"] == "superseded"

        deferred = runtime.owners.human_collaboration.respond_to_human_request(
            successor["request_ref"],
            decision="deferred",
            facts={},
            note="I will do this later.",
            idempotency_key="defer-offline",
        )
        evaluated = runtime.owners.research_graph.evaluate_human_request(
            successor["request_ref"],
            response_refs=(deferred["response_ref"],),
            decision="needs_input",
            reason_code="offline_action_not_performed",
            accepted_evidence_refs=(),
            idempotency_key="evaluate-deferred",
        )
        assert evaluated["disposition"] is None

        raw_response = runtime.owners.human_collaboration.respond_to_human_request(
            successor["request_ref"],
            decision="provided",
            facts={"token": "ghp_test_only_credential"},
            note="My password is test-only-password.",
            idempotency_key="raw-offline-response",
        )
        assert raw_response["facts"] == {"token": "ghp_test_only_credential"}
        assert raw_response["note"] == "My password is test-only-password."

        declined = runtime.owners.human_collaboration.respond_to_human_request(
            successor["request_ref"],
            decision="declined",
            facts={},
            note="Do not perform this action.",
            idempotency_key="decline-offline",
        )
        terminal = runtime.owners.research_graph.evaluate_human_request(
            successor["request_ref"],
            response_refs=(declined["response_ref"],),
            decision="declined",
            reason_code="human_declined_exact_obligation",
            accepted_evidence_refs=(),
            idempotency_key="evaluate-declined",
        )
        assert terminal["disposition"]["decision"] == "declined"

        recurrence = runtime.owners.research_graph.open_human_request(
            request_kind=successor["kind"],
            obligation=successor["obligation"],
            business_purpose=successor["business_purpose"],
            target_assertion=successor["target_assertion"],
            acceptance_conditions=tuple(successor["acceptance_conditions"]),
            direct_waiter={
                "waiter_ref": "target_run_2",
                "generation": 1,
                "target_assertion": successor["target_assertion"],
                "wait_scope": "quest",
                "other_blockers": [],
            },
            idempotency_key="recur-offline",
        )
        assert recurrence["request_id"] != successor["request_id"]
        assert recurrence["revision"] == 1
    finally:
        runtime.close()


def test_business_content_is_unfiltered_but_identity_fields_remain_guarded(
    tmp_path,
) -> None:
    provider = _DeterministicDraftingProvider()
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "owner-human-request-secret-rejection"),
        proposal_drafter=provider,
        intent_drafting_provider=provider,
    )
    owner = runtime.owners.agent_runtime
    try:
        with pytest.raises(OwnerConflict, match="idempotency_key_invalid"):
            owner.open_human_request(
                request_kind="library_reconnect",
                obligation="Reconnect the institution-backed route.",
                business_purpose="Resume exact acquisition.",
                target_assertion={"session_ref": "safe-session"},
                acceptance_conditions=("The exact route is ready.",),
                direct_waiter=_waiter("secret-idempotency-waiter"),
                idempotency_key="password=hunter2",
            )
        with pytest.raises(OwnerConflict, match="human_request_secret_forbidden"):
            owner.open_human_request(
                request_kind="library_reconnect",
                obligation="Reconnect the institution-backed route.",
                business_purpose="Resume exact acquisition.",
                target_assertion={"session_ref": "safe-session"},
                acceptance_conditions=("The exact route is ready.",),
                direct_waiter={
                    **_waiter("secret-blocker-waiter"),
                    "other_blockers": ["password=must-not-persist"],
                },
                idempotency_key="secret-blocker-open",
            )

        request = _open_library_request(
            owner,
            "secret-evaluation-waiter",
            "secret-evaluation-open",
        )
        with pytest.raises(OwnerConflict, match="idempotency_key_invalid"):
            runtime.owners.human_collaboration.respond_to_human_request(
                request["request_ref"],
                decision="provided",
                facts={"route_status": "ready"},
                note="The exact route is ready.",
                idempotency_key="password=hunter2",
            )
        response = runtime.owners.human_collaboration.respond_to_human_request(
            request["request_ref"],
            decision="provided",
            facts={"api_token": "ghp_test_only_credential"},
            note="Use password=test-only-password.",
            idempotency_key="raw-evaluation-response",
        )
        replay = runtime.owners.human_collaboration.respond_to_human_request(
            request["request_ref"],
            decision="provided",
            facts={"api_token": "ghp_test_only_credential"},
            note="Use password=test-only-password.",
            idempotency_key="raw-evaluation-response",
        )
        assert replay == response
        observed = owner.query_human_request(request["request_ref"])
        assert observed is not None
        assert observed["responses"] == [response]
        assert observed["response_rejections"] == []
        with pytest.raises(OwnerConflict, match="idempotency_conflict"):
            runtime.owners.human_collaboration.respond_to_human_request(
                request["request_ref"],
                decision="provided",
                facts={"route_status": "ready"},
                note="Different content must not reuse the same identity.",
                idempotency_key="raw-evaluation-response",
            )
        with pytest.raises(OwnerConflict, match="human_request_secret_forbidden"):
            owner.evaluate_human_request(
                request["request_ref"],
                response_refs=(response["response_ref"],),
                decision="satisfied",
                reason_code="password=must-not-persist",
                accepted_evidence_refs=("safe-evidence-ref",),
                idempotency_key="secret-evaluation",
            )
    finally:
        runtime.close()


def _root_effect_binding(
    *, generation: int = 3, task_index: int = 1
) -> dict[str, object]:
    return {
        "quest_ref": "quest_root_human_request_1",
        "task_ref": f"deepfetch_run_root_human_request_{task_index}",
        "root_session_ref": (
            f"deepfetch_session_root_human_request_{task_index}"
        ),
        "operation_id": ROOT_AGENT_HUMAN_REQUEST_OPERATION_IDS[0],
        "attempt_ref": f"deepfetch_attempt_root_human_request_{task_index}",
        "generation": generation,
        "request_owner": "agent_runtime",
        "root_kind": "deepfetch",
        "phase": "primary",
        "fence_ref": f"deepfetch_fence_root_human_request_{task_index}",
        "runtime_binding_hash": "a" * 64,
    }


def _open_root_human_request_effect(
    owner,
    *,
    effect_key: str,
    effect_id: str,
    request_kind: str = "library_reconnect",
    generation: int = 3,
    task_index: int = 1,
    predecessor_request_ref: str | None = None,
) -> dict[str, object]:
    binding = _root_effect_binding(
        generation=generation, task_index=task_index
    )
    target = {
        "schema_ref": "meta-research/root-agent-human-request-target/v1",
        "root": {
            "run_kind": "deepfetch",
            "run_ref": binding["task_ref"],
            "attempt_ref": binding["attempt_ref"],
            "root_session_ref": binding["root_session_ref"],
            "fence_ref": binding["fence_ref"],
            "waiter_generation": generation,
        },
        "condition": {"route": "literature_access"},
    }
    return owner.open_human_request_effect(
        effect_key=effect_key,
        effect_id=effect_id,
        operation_binding=binding,
        predecessor_request_ref=predecessor_request_ref,
        request_kind=request_kind,
        obligation="Restore a usable literature route for this exact task.",
        business_purpose="Resume the exact blocked Root task.",
        target_assertion=target,
        acceptance_conditions=("A safe current route is selected.",),
        direct_waiter={
            "waiter_ref": f"root_run:{binding['task_ref']}",
            "generation": generation,
            "target_assertion": target,
            "wait_scope": "local",
            "other_blockers": [],
        },
        quest_ref=str(binding["quest_ref"]),
    )


def _allow_current_root_human_request_scope(
    monkeypatch: pytest.MonkeyPatch,
    owner,
    *,
    task_index: int = 1,
    waiter_generation: int = 3,
) -> None:
    binding = _root_effect_binding(task_index=task_index)

    def verify_scope(**values: object) -> dict[str, object]:
        assert values == {
            "root_kind": "deepfetch",
            "run_ref": binding["task_ref"],
            "attempt_ref": binding["attempt_ref"],
            "root_session_ref": binding["root_session_ref"],
            "fence_ref": binding["fence_ref"],
            "runtime_binding_hash": binding["runtime_binding_hash"],
            "allowed_statuses": values["allowed_statuses"],
        }
        assert values["allowed_statuses"] in {
            frozenset({"running"}),
            frozenset({"suspended"}),
        }
        return {
            "run_kind": "deepfetch",
            "quest_ref": binding["quest_ref"],
            "waiter_ref": f"root_run:{binding['task_ref']}",
            "waiter_generation": waiter_generation,
        }

    monkeypatch.setattr(owner, "_verify_root_agent_runtime_scope", verify_scope)


def _grant_capability_authorization(
    human,
    *,
    scope_ref: str,
    requirement: dict[str, object],
    key: str,
) -> tuple[dict[str, object], dict[str, object]]:
    drafted = human.create_command_draft(
        scope_ref,
        {
            "command_kind": "capability_authorization",
            "payload": {
                "capability": requirement["capability"],
                "decision": "granted",
                "scope": requirement["scope"],
            },
        },
        f"{key}-draft",
    )
    preview = human.preview_command(
        drafted["intent_id"],
        drafted["draft_revision"],
        drafted["draft_hash"],
        f"{key}-preview",
    )["impact_preview"]
    confirmation = human.confirm_command(
        drafted["intent_id"],
        drafted["draft_revision"],
        drafted["draft_hash"],
        preview["preview_ref"],
        preview["preview_hash"],
        f"{key}-confirm",
    )
    authorization = human.decide_capability_authorization(
        scope_ref,
        {
            "capability": requirement["capability"],
            "decision": "granted",
            "scope": requirement["scope"],
            "confirmation_receipt_ref": confirmation["confirmation_receipt"][
                "receipt_ref"
            ],
        },
        f"{key}-authorize",
    )
    return confirmation, authorization


def _seed_root_human_request_task(runtime, *, task_index: int = 1) -> None:
    binding = _root_effect_binding(task_index=task_index)
    with runtime._database.write() as connection:
        connection.execute(
            text(
                "INSERT INTO ar_run_controls (run_ref, run_kind, quest_ref, "
                "cycle_ref, epoch, status, attempt_ref, root_session_ref, "
                "fence_ref, control_revision, safe_point_ref, terminal_reason, "
                "cleanup_status, updated_at) VALUES (:run_ref, 'deepfetch', "
                ":quest_ref, NULL, NULL, 'running', :attempt_ref, "
                ":root_session_ref, :fence_ref, 1, NULL, NULL, 'none', :now)"
            ),
            {**binding, "run_ref": binding["task_ref"], "now": time.time()},
        )


@pytest.mark.parametrize("root_kind", ("acquisition", "companion"))
def test_external_root_task_scope_drives_exact_human_request_lifecycle(
    tmp_path,
    root_kind: str,
) -> None:
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / f"external-root-scope-{root_kind}")
    )
    owner = runtime.owners.agent_runtime
    scope = {
        "quest_ref": f"quest_external_root_{root_kind}",
        "run_ref": f"{root_kind}_task_external_root_1",
        "attempt_ref": f"{root_kind}_attempt_external_root_1",
        "root_session_ref": f"{root_kind}_session_external_root_1",
        "fence_ref": f"{root_kind}_fence_external_root_1",
        "runtime_binding_hash": "b" * 64,
        "generation": 2,
    }
    binding = {
        "quest_ref": scope["quest_ref"],
        "task_ref": scope["run_ref"],
        "root_session_ref": scope["root_session_ref"],
        "operation_id": ROOT_AGENT_HUMAN_REQUEST_OPERATION_IDS[0],
        "attempt_ref": scope["attempt_ref"],
        "generation": scope["generation"],
        "request_owner": "agent_runtime",
        "root_kind": root_kind,
        "phase": "primary",
        "fence_ref": scope["fence_ref"],
        "runtime_binding_hash": scope["runtime_binding_hash"],
    }
    target = {
        "schema_ref": "meta-research/root-agent-human-request-target/v1",
        "root": {
            "run_kind": root_kind,
            "run_ref": scope["run_ref"],
            "attempt_ref": scope["attempt_ref"],
            "root_session_ref": scope["root_session_ref"],
            "fence_ref": scope["fence_ref"],
            "waiter_generation": scope["generation"],
        },
        "condition": {"operator_choice": "continue_without_optional_input"},
    }
    try:
        registered = owner.register_external_root_task_scope(
            root_kind=root_kind,
            root_runtime_scope=scope,
        )
        assert owner.register_external_root_task_scope(
            root_kind=root_kind,
            root_runtime_scope=scope,
        ) == registered
        assert owner.verify_root_agent_runtime_scope(
            root_kind=root_kind,
            run_ref=str(scope["run_ref"]),
            attempt_ref=str(scope["attempt_ref"]),
            root_session_ref=str(scope["root_session_ref"]),
            fence_ref=str(scope["fence_ref"]),
            runtime_binding_hash=str(scope["runtime_binding_hash"]),
        ) == {
            "run_kind": root_kind,
            "quest_ref": scope["quest_ref"],
            "waiter_ref": f"root_run:{scope['run_ref']}",
            "waiter_generation": scope["generation"],
        }
        human_request = owner.open_human_request_effect(
            effect_key=f"mcp-effect:{root_kind}-external-root",
            effect_id=f"{root_kind}-external-root",
            operation_binding=binding,
            predecessor_request_ref=None,
            request_kind="offline_action",
            obligation="Choose how this exact external Root task should continue.",
            business_purpose="Resume the exact suspended Root task.",
            target_assertion=target,
            acceptance_conditions=("The operator supplies a disposition.",),
            direct_waiter={
                "waiter_ref": f"root_run:{scope['run_ref']}",
                "generation": scope["generation"],
                "target_assertion": target,
                "wait_scope": "local",
                "other_blockers": [],
            },
            quest_ref=str(scope["quest_ref"]),
        )
        suspended = owner.query_managed_run(str(scope["run_ref"]))
        assert suspended is not None and suspended["status"] == "suspended"
        with pytest.raises(
            OwnerConflict, match="external_root_task_scope_suspended"
        ):
            owner.complete_external_root_task_scope(
                root_kind=root_kind,
                root_runtime_scope=scope,
            )
        assert owner.verify_root_agent_human_request_reconcile_scope(
            root_kind=root_kind,
            run_ref=str(scope["run_ref"]),
            attempt_ref=str(scope["attempt_ref"]),
            root_session_ref=str(scope["root_session_ref"]),
            fence_ref=str(scope["fence_ref"]),
            runtime_binding_hash=str(scope["runtime_binding_hash"]),
        )["waiter_generation"] == scope["generation"]

        runtime.owners.human_collaboration.respond_to_human_request(
            str(human_request["request_ref"]),
            decision="deferred",
            facts={},
            note="Continue without the optional input.",
            idempotency_key=f"{root_kind}-external-root-response",
        )
        disposed = owner.query_human_request(str(human_request["request_ref"]))
        assert disposed is not None
        assert disposed["status"] == "unsatisfied"
        assert disposed["direct_waiters"][0]["status"] == "consumed"
        resumed = owner.query_managed_run(str(scope["run_ref"]))
        assert resumed is not None and resumed["status"] == "running"

        completed = owner.complete_external_root_task_scope(
            root_kind=root_kind,
            root_runtime_scope=scope,
        )
        assert completed["status"] == "completed"
        assert owner.complete_external_root_task_scope(
            root_kind=root_kind,
            root_runtime_scope=scope,
        ) == completed
    finally:
        runtime.close()


def test_external_root_successor_resumes_on_next_physical_attempt(
    tmp_path,
) -> None:
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "external-root-successor-next-attempt")
    )
    owner = runtime.owners.agent_runtime
    human = runtime.owners.human_collaboration
    first_scope: dict[str, object] = {
        "quest_ref": "quest_external_root_successor",
        "run_ref": "companion_task_external_root_successor",
        "attempt_ref": "companion_attempt_external_root_successor_1",
        "root_session_ref": "companion_session_external_root_successor",
        "fence_ref": "companion_fence_external_root_successor_1",
        "runtime_binding_hash": "d" * 64,
        "generation": 1,
    }

    def open_request(
        scope: dict[str, object],
        *,
        effect_suffix: str,
        predecessor_request_ref: str | None,
    ) -> dict[str, object]:
        binding = {
            "quest_ref": scope["quest_ref"],
            "task_ref": scope["run_ref"],
            "root_session_ref": scope["root_session_ref"],
            "operation_id": ROOT_AGENT_HUMAN_REQUEST_OPERATION_IDS[0],
            "attempt_ref": scope["attempt_ref"],
            "generation": scope["generation"],
            "request_owner": "agent_runtime",
            "root_kind": "companion",
            "phase": "reply",
            "fence_ref": scope["fence_ref"],
            "runtime_binding_hash": scope["runtime_binding_hash"],
        }
        target = {
            "schema_ref": "meta-research/root-agent-human-request-target/v1",
            "root": {
                "run_kind": "companion",
                "run_ref": scope["run_ref"],
                "attempt_ref": scope["attempt_ref"],
                "root_session_ref": scope["root_session_ref"],
                "fence_ref": scope["fence_ref"],
                "waiter_generation": scope["generation"],
            },
            "condition": {"operator_choice": "continue_without_optional_input"},
        }
        return owner.open_human_request_effect(
            effect_key=f"mcp-effect:external-root-successor-{effect_suffix}",
            effect_id=f"external-root-successor-{effect_suffix}",
            operation_binding=binding,
            predecessor_request_ref=predecessor_request_ref,
            request_kind="offline_action",
            obligation="Choose how this exact Companion task should continue.",
            business_purpose="Resume the exact suspended Companion task.",
            target_assertion=target,
            acceptance_conditions=("The operator supplies a disposition.",),
            direct_waiter={
                "waiter_ref": f"root_run:{scope['run_ref']}",
                "generation": scope["generation"],
                "target_assertion": target,
                "wait_scope": "local",
                "other_blockers": [],
            },
            quest_ref=str(scope["quest_ref"]),
        )

    try:
        owner.register_external_root_task_scope(
            root_kind="companion",
            root_runtime_scope=first_scope,
        )
        predecessor = open_request(
            first_scope,
            effect_suffix="first",
            predecessor_request_ref=None,
        )
        human.respond_to_human_request(
            str(predecessor["request_ref"]),
            decision="deferred",
            facts={},
            note="Continue without this optional input.",
            idempotency_key="external-root-successor-first-response",
        )

        intermediate_scope = {
            **first_scope,
            "attempt_ref": "companion_attempt_external_root_successor_2",
            "fence_ref": "companion_fence_external_root_successor_2",
            "generation": 2,
        }
        owner.register_external_root_task_scope(
            root_kind="companion",
            root_runtime_scope=intermediate_scope,
        )
        owner.complete_external_root_task_scope(
            root_kind="companion",
            root_runtime_scope=intermediate_scope,
        )
        next_scope = {
            **first_scope,
            "attempt_ref": "companion_attempt_external_root_successor_3",
            "fence_ref": "companion_fence_external_root_successor_3",
            "generation": 3,
        }
        owner.register_external_root_task_scope(
            root_kind="companion",
            root_runtime_scope=next_scope,
        )
        successor = open_request(
            next_scope,
            effect_suffix="second",
            predecessor_request_ref=str(predecessor["request_ref"]),
        )
        human.respond_to_human_request(
            str(successor["request_ref"]),
            decision="deferred",
            facts={},
            note="Continue without this optional input again.",
            idempotency_key="external-root-successor-second-response",
        )

        current = owner.query_human_request(str(successor["request_ref"]))
        assert current is not None
        assert current["status"] == "unsatisfied"
        assert current["direct_waiters"][0]["status"] == "consumed"
        managed = owner.query_managed_run(str(next_scope["run_ref"]))
        assert managed is not None and managed["status"] == "running"
    finally:
        runtime.close()


def test_external_root_task_scope_rejects_missing_quest_and_identity_drift(
    tmp_path,
) -> None:
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "external-root-scope-invalid")
    )
    owner = runtime.owners.agent_runtime
    scope = {
        "quest_ref": "quest_external_root_invalid",
        "run_ref": "acquisition_task_external_root_invalid",
        "attempt_ref": "acquisition_attempt_external_root_invalid",
        "root_session_ref": "acquisition_session_external_root_invalid",
        "fence_ref": "acquisition_fence_external_root_invalid",
        "runtime_binding_hash": "c" * 64,
        "generation": 1,
    }
    try:
        with pytest.raises(OwnerConflict, match="external_root_task_scope_invalid"):
            owner.register_external_root_task_scope(
                root_kind="acquisition",
                root_runtime_scope={**scope, "quest_ref": None},
            )
        owner.register_external_root_task_scope(
            root_kind="acquisition",
            root_runtime_scope=scope,
        )
        with pytest.raises(OwnerConflict, match="external_root_task_scope_conflict"):
            owner.register_external_root_task_scope(
                root_kind="acquisition",
                root_runtime_scope={**scope, "fence_ref": "stale-fence"},
            )
    finally:
        runtime.close()


def test_root_human_request_open_effect_is_frozen_and_not_contract_reused(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "root-human-request-open-effect")
    )
    owner = runtime.owners.agent_runtime
    _seed_root_human_request_task(runtime)
    _seed_root_human_request_task(runtime, task_index=2)
    _allow_current_root_human_request_scope(monkeypatch, owner)
    try:
        first = _open_root_human_request_effect(
            owner,
            effect_key="mcp-effect:root-human-open-1",
            effect_id="root-human-open-1",
        )
        replay = _open_root_human_request_effect(
            owner,
            effect_key="mcp-effect:root-human-open-1",
            effect_id="root-human-open-1",
        )
        _allow_current_root_human_request_scope(
            monkeypatch, owner, task_index=2
        )
        second = _open_root_human_request_effect(
            owner,
            effect_key="mcp-effect:root-human-open-2",
            effect_id="root-human-open-2",
            task_index=2,
        )

        assert replay["request_ref"] == first["request_ref"]
        assert replay["open_receipt"] == first["open_receipt"]
        assert replay["open_effect"] == first["open_effect"]
        assert second["request_ref"] != first["request_ref"]
        assert len(first["direct_waiters"]) == 1
        assert len(second["direct_waiters"]) == 1
        assert first["request_owner"] == "agent_runtime"
        assert first["operation"] == ROOT_AGENT_HUMAN_REQUEST_OPERATION_IDS[0]
        assert first["generation"] == 3
        assert first["open_effect"]["effect_id"] == "root-human-open-1"
        assert first["open_effect"]["yield"]["status"] == "yielded"

        reconciled = owner.reconcile_human_request_effect(
            "mcp-effect:root-human-open-1"
        )
        assert reconciled["request_ref"] == first["request_ref"]
        assert reconciled["open_receipt"] == first["open_receipt"]
        with pytest.raises(OwnerConflict, match="effect_not_found"):
            owner.reconcile_human_request_effect("mcp-effect:missing")
    finally:
        runtime.close()


def test_root_human_request_provided_releases_and_resumes_exactly_once(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_root = prepare_data_root(tmp_path / "root-human-request-provided")
    runtime = build_production_runtime(data_root)
    owner = runtime.owners.agent_runtime
    _seed_root_human_request_task(runtime)
    _allow_current_root_human_request_scope(monkeypatch, owner)
    try:
        request = _open_root_human_request_effect(
            owner,
            effect_key="mcp-effect:root-human-provided",
            effect_id="root-human-provided",
        )
        assert owner.query_managed_run(
            str(request["open_effect"]["operation_binding"]["task_ref"])
        )["status"] == "suspended"
        response = runtime.owners.human_collaboration.respond_to_human_request(
            request["request_ref"],
            decision="provided",
            facts={"route": "oa_only"},
            note="Continue with lawful open-access sources.",
            idempotency_key="root-human-provided-response",
        )
        current = owner.query_human_request(request["request_ref"])
        assert current is not None
        assert current["responses"] == [response]
        assert current["disposition"]["decision"] == "satisfied"
        assert current["direct_waiters"][0]["status"] == "consumed"
        validation = current["direct_waiters"][0]["resume_validation"]
        assert validation["status"] == "released"
        assert validation["started_work"] is True
        consumption = validation["consumption"]
        assert consumption["work_ref"] == request["open_effect"][
            "operation_binding"
        ]["task_ref"]
        assert owner.query_managed_run(str(consumption["work_ref"]))[
            "status"
        ] == "running"

        replay = runtime.owners.human_collaboration.respond_to_human_request(
            request["request_ref"],
            decision="provided",
            facts={"route": "oa_only"},
            note="Continue with lawful open-access sources.",
            idempotency_key="root-human-provided-response",
        )
        replayed = owner.query_human_request(request["request_ref"])
        assert replay == response
        assert replayed is not None
        assert (
            replayed["direct_waiters"][0]["resume_validation"]["consumption"]
            == consumption
        )
    finally:
        runtime.close()


def test_external_human_request_natural_language_response_resumes_responsible_agent(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_kind = "external_material_api_access"
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / f"root-human-natural-response-{request_kind}")
    )
    owner = runtime.owners.agent_runtime
    _seed_root_human_request_task(runtime)
    _allow_current_root_human_request_scope(monkeypatch, owner)
    try:
        request = _open_root_human_request_effect(
            owner,
            effect_key=f"mcp-effect:root-human-natural-{request_kind}",
            effect_id=f"root-human-natural-{request_kind}",
            request_kind=request_kind,
        )
        response = runtime.owners.human_collaboration.respond_to_human_request(
            request["request_ref"],
            decision="provided",
            facts={"local_path": "/srv/research/operator-result"},
            note="I could not finish; use the local path or choose another route.",
            idempotency_key=f"root-human-natural-response-{request_kind}",
        )

        current = owner.query_human_request(str(request["request_ref"]))
        assert current is not None
        assert current["responses"] == [response]
        assert current["status"] == "satisfied"
        assert current["evaluation"]["accepted_evidence_refs"] == []
        assert current["direct_waiters"][0]["status"] == "consumed"
        assert owner.query_managed_run(
            str(request["open_effect"]["operation_binding"]["task_ref"])
        )["status"] == "running"
    finally:
        runtime.close()


def test_agent_issued_system_operation_help_retry_resumes_exact_agent(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "root-system-operation-help")
    )
    owner = runtime.owners.agent_runtime
    _seed_root_human_request_task(runtime)
    _allow_current_root_human_request_scope(monkeypatch, owner)
    try:
        request = _open_root_human_request_effect(
            owner,
            effect_key="mcp-effect:root-system-operation-help",
            effect_id="root-system-operation-help",
            request_kind="system_operation_help",
        )
        assert owner.query_snapshot().facts["human_request_count"] == 1
        binding = request["open_effect"]["operation_binding"]
        monkeypatch.setattr(
            owner,
            "_root_human_request_resume_checkpoint_ready",
            lambda _binding: False,
        )
        with TestClient(
            create_app(
                runtime,
                base_url="http://testserver",
                control_key="control-key",
            ),
            base_url="http://testserver",
        ) as client:
            bootstrap = runtime.authentication.issue_bootstrap_token()
            authenticated = client.post(
                "/auth/bootstrap",
                headers={"Origin": "http://testserver"},
                json={"token": bootstrap},
            )
            assert authenticated.status_code == 200
            headers = {
                "Origin": "http://testserver",
                "X-CSRF-Token": authenticated.json()["csrf_token"],
                "Idempotency-Key": "root-system-operation-help-retry",
            }
            retry_url = f"/api/v1/human-requests/{request['request_ref']}/retry"
            processing = client.post(retry_url, headers=headers, json={})
            assert processing.status_code == 200, processing.json()
            assert processing.json()["retry"] == {"status": "processing"}

            pending = owner.query_human_request(str(request["request_ref"]))
            assert pending is not None
            assert pending["status"] == "open"
            assert pending["direct_waiters"][0]["status"] == "blocked"
            assert owner.query_managed_run(str(binding["task_ref"]))[
                "status"
            ] == "suspended"

            processing_replay = client.post(retry_url, headers=headers, json={})
            assert processing_replay.status_code == 200, processing_replay.json()
            assert processing_replay.json()["retry"] == {"status": "processing"}
            assert len(
                owner.query_human_request(str(request["request_ref"]))["responses"]
            ) == 1

            other_headers = {
                **headers,
                "Idempotency-Key": "root-system-operation-help-retry-2",
            }
            other_processing = client.post(
                retry_url, headers=other_headers, json={}
            )
            assert other_processing.status_code == 200, other_processing.json()
            assert other_processing.json()["retry"] == {"status": "processing"}
            other_replay = client.post(retry_url, headers=other_headers, json={})
            assert other_replay.status_code == 200, other_replay.json()
            assert len(
                owner.query_human_request(str(request["request_ref"]))["responses"]
            ) == 2

            runtime.owners.human_collaboration.respond_to_human_request(
                str(request["request_ref"]),
                decision="provided",
                facts={"action": "comment"},
                note="Do not use the earlier Retry yet.",
                idempotency_key="root-system-operation-help-later-comment",
            )

            monkeypatch.setattr(
                owner,
                "_root_human_request_resume_checkpoint_ready",
                lambda _binding: True,
            )
            succeeded = client.post(
                retry_url,
                headers={
                    **headers,
                    "Idempotency-Key": "root-system-operation-help-retry-3",
                },
                json={},
            )
            assert succeeded.status_code == 200, succeeded.json()
            assert succeeded.json()["retry"] == {"status": "succeeded"}
            terminal_replay = client.post(
                retry_url,
                headers={
                    **headers,
                    "Idempotency-Key": "root-system-operation-help-retry-3",
                },
                json={},
            )
            assert terminal_replay.status_code == 200, terminal_replay.json()
            assert terminal_replay.json() == succeeded.json()

        current = owner.query_human_request(str(request["request_ref"]))
        assert current is not None
        assert len(current["responses"]) == 4
        assert current["responses"][-1]["facts"] == {"action": "retry"}
        assert current["status"] == "satisfied"
        assert current["evaluation"]["reason"]["code"] == (
            "system_operation_retry_requested"
        )
        consumption = current["direct_waiters"][0]["resume_validation"][
            "consumption"
        ]
        assert consumption["work_ref"] == binding["task_ref"]
        resumed = owner.query_managed_run(str(binding["task_ref"]))
        assert resumed["status"] == "running"
        assert resumed["root_session_ref"] == binding["root_session_ref"]

        with pytest.raises(OwnerConflict, match="human_request_predecessor_invalid"):
            _open_root_human_request_effect(
                owner,
                effect_key="mcp-effect:different-system-operation-help:r2",
                effect_id="different-system-operation-help",
                request_kind="system_operation_help",
                generation=4,
                predecessor_request_ref=str(request["request_ref"]),
            )

        repeated_failure = _open_root_human_request_effect(
            owner,
            effect_key="mcp-effect:root-system-operation-help:r2",
            effect_id="root-system-operation-help",
            request_kind="system_operation_help",
            generation=4,
            predecessor_request_ref=str(request["request_ref"]),
        )
        assert repeated_failure["request_id"] == request["request_id"]
        assert repeated_failure["request_ref"] == f"{request['request_id']}:r2"
        assert repeated_failure["revision"] == 2
        assert repeated_failure["predecessor_request_ref"] == request["request_ref"]
        predecessor = owner.query_human_request(str(request["request_ref"]))
        assert predecessor is not None
        assert predecessor["current"] is False
        assert predecessor["status"] == "satisfied"
        assert predecessor["successor_request_ref"] == repeated_failure["request_ref"]
        assert repeated_failure["current"] is True
        assert repeated_failure["status"] == "open"
        assert repeated_failure["direct_waiters"][0]["status"] == "blocked"
        assert owner.query_snapshot().facts["human_request_count"] == 1
        assert owner.query_managed_run(str(binding["task_ref"]))[
            "status"
        ] == "suspended"
    finally:
        runtime.close()


def test_agent_issued_system_operation_help_non_retry_does_not_resume(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "root-system-operation-help-non-retry")
    )
    owner = runtime.owners.agent_runtime
    _seed_root_human_request_task(runtime)
    _allow_current_root_human_request_scope(monkeypatch, owner)
    try:
        request = _open_root_human_request_effect(
            owner,
            effect_key="mcp-effect:root-system-operation-help-non-retry",
            effect_id="root-system-operation-help-non-retry",
            request_kind="system_operation_help",
        )
        runtime.owners.human_collaboration.respond_to_human_request(
            request["request_ref"],
            decision="provided",
            facts={"action": "comment"},
            note="Do not retry yet.",
            idempotency_key="root-system-operation-help-non-retry-response",
        )

        current = owner.query_human_request(str(request["request_ref"]))
        assert current is not None
        assert current["status"] == "open"
        assert current["evaluation"]["decision"] == "needs_input"
        assert current["direct_waiters"][0]["status"] == "blocked"
        assert owner.query_managed_run(
            str(request["open_effect"]["operation_binding"]["task_ref"])
        )["status"] == "suspended"
    finally:
        runtime.close()


def test_component_retry_consumes_only_exact_system_operation_help_waiter(
    tmp_path,
) -> None:
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "component-system-operation-help")
    )
    owner = runtime.owners.agent_runtime
    target = {
        "operation_ref": "writing_provider_operation_1",
        "retry_action": "retry_writing_provider",
    }
    try:
        request = owner.open_human_request(
            request_kind="system_operation_help",
            obligation="Retry the failed Writing provider operation.",
            business_purpose="Resume only the dependent Writing task.",
            target_assertion=target,
            acceptance_conditions=("The exact provider retry succeeds.",),
            direct_waiter={
                "waiter_ref": "writing_retry:writing_provider_operation_1",
                "generation": 1,
                "target_assertion": target,
                "wait_scope": "local",
                "other_blockers": [],
            },
            idempotency_key="open-component-system-operation-help",
        )
        response = runtime.owners.human_collaboration.respond_to_human_request(
            request["request_ref"],
            decision="provided",
            facts={"action": "retry"},
            note="Retry the exact failed component.",
            idempotency_key="respond-component-system-operation-help",
        )
        owner.evaluate_human_request(
            request["request_ref"],
            response_refs=(response["response_ref"],),
            decision="satisfied",
            reason_code="system_operation_retry_succeeded",
            accepted_evidence_refs=(),
            idempotency_key="evaluate-component-system-operation-help",
        )
        owner.validate_human_request_waiter(
            request["request_ref"],
            waiter_ref="writing_retry:writing_provider_operation_1",
            generation=1,
            target_assertion=target,
            other_blockers=(),
            idempotency_key="release-component-system-operation-help",
        )

        with pytest.raises(
            OwnerConflict, match="system_operation_help_waiter_invalid"
        ):
            owner.consume_system_operation_help_waiter(
                request["request_ref"],
                waiter_ref="writing_retry:other_operation",
                generation=1,
                work_ref="writing_provider_operation_1",
                work_hash="b" * 64,
            )
        consumption = owner.consume_system_operation_help_waiter(
            request["request_ref"],
            waiter_ref="writing_retry:writing_provider_operation_1",
            generation=1,
            work_ref="writing_provider_operation_1",
            work_hash="b" * 64,
        )
        assert consumption["work_ref"] == "writing_provider_operation_1"
        assert owner.query_human_request(request["request_ref"])[
            "direct_waiters"
        ][0]["status"] == "consumed"
    finally:
        runtime.close()


@pytest.mark.parametrize("decision", ("declined", "deferred"))
def test_root_human_request_unsatisfied_still_releases_exact_waiter(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    decision: str,
) -> None:
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / f"root-human-request-{decision}")
    )
    owner = runtime.owners.agent_runtime
    _seed_root_human_request_task(runtime)
    _allow_current_root_human_request_scope(monkeypatch, owner)
    try:
        request = _open_root_human_request_effect(
            owner,
            effect_key=f"mcp-effect:root-human-{decision}",
            effect_id=f"root-human-{decision}",
        )
        runtime.owners.human_collaboration.respond_to_human_request(
            request["request_ref"],
            decision=decision,
            facts={},
            note="Use an alternative route.",
            idempotency_key=f"root-human-{decision}-response",
        )

        current = owner.query_human_request(request["request_ref"])
        assert current is not None
        assert current["status"] == "unsatisfied"
        assert current["evaluation"]["decision"] == "unsatisfied"
        assert current["disposition"]["decision"] == "unsatisfied"
        assert current["direct_waiters"][0]["status"] == "consumed"
        assert (
            current["direct_waiters"][0]["resume_validation"]["status"]
            == "released"
        )
    finally:
        runtime.close()


def test_root_human_request_invalid_provided_response_does_not_release(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "root-human-request-invalid-proof")
    )
    owner = runtime.owners.agent_runtime
    _seed_root_human_request_task(runtime)
    _allow_current_root_human_request_scope(monkeypatch, owner)
    try:
        request = _open_root_human_request_effect(
            owner,
            effect_key="mcp-effect:root-human-invalid-proof",
            effect_id="root-human-invalid-proof",
        )
        runtime.owners.human_collaboration.respond_to_human_request(
            request["request_ref"],
            decision="provided",
            facts={"route": "institutional_browser_reconnected"},
            note="No exact preflight proof was supplied.",
            idempotency_key="root-human-invalid-proof-response",
        )

        current = owner.query_human_request(request["request_ref"])
        assert current is not None
        assert current["status"] == "open"
        assert current["disposition"] is None
        assert current["evaluation"]["decision"] == "needs_input"
        assert current["direct_waiters"][0]["status"] == "blocked"
        assert owner.query_managed_run(
            str(request["open_effect"]["operation_binding"]["task_ref"])
        )["status"] == "suspended"
    finally:
        runtime.close()


@pytest.mark.parametrize(
    "case",
    ("exact", "partial", "forged"),
)
def test_root_human_request_accepted_asset_proof_uses_public_flat_facts(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / f"root-human-asset-{case}")
    )
    owner = runtime.owners.agent_runtime
    _seed_root_human_request_task(runtime)
    _allow_current_root_human_request_scope(monkeypatch, owner)
    intake = runtime.owners.research_memory.submit_asset_intake(
        AssetIntakeRequest(
            source_kind="file",
            custody_mode="managed",
            display_name="operator-result.txt",
            media_type="text/plain",
            content=b"accepted operator result",
            provenance={"source": "human_request_test"},
            asynchronous=False,
        ),
        idempotency_key=f"root-human-asset-{case}",
    )
    assert intake.asset is not None
    asset = intake.asset
    fact_prefix = "material"
    binding = _root_effect_binding()
    target = {
        "schema_ref": "meta-research/root-agent-human-request-target/v1",
        "root": {
            "run_kind": "deepfetch",
            "run_ref": binding["task_ref"],
            "attempt_ref": binding["attempt_ref"],
            "root_session_ref": binding["root_session_ref"],
            "fence_ref": binding["fence_ref"],
            "waiter_generation": binding["generation"],
        },
        "condition": {"accepted_asset": fact_prefix},
    }
    try:
        request = owner.open_human_request_effect(
            effect_key=f"mcp-effect:root-human-asset-{case}",
            effect_id=f"root-human-asset-{case}",
            operation_binding=binding,
            predecessor_request_ref=None,
            request_kind="external_material_api_access",
            obligation="Provide one exact accepted asset.",
            business_purpose="Resume the exact blocked Root task.",
            target_assertion=target,
            acceptance_conditions=("The accepted asset binding is exact.",),
            direct_waiter={
                "waiter_ref": f"root_run:{binding['task_ref']}",
                "generation": binding["generation"],
                "target_assertion": target,
                "wait_scope": "local",
                "other_blockers": [],
            },
            quest_ref=str(binding["quest_ref"]),
        )
        facts: dict[str, object] = {
            f"{fact_prefix}_source_ref": asset.memory_ref,
            f"{fact_prefix}_version_ref": asset.version_ref,
            f"{fact_prefix}_content_hash": asset.content_hash,
            f"{fact_prefix}_manifest_hash": asset.manifest_hash,
            f"{fact_prefix}_acceptance_receipt_ref": asset.receipt.receipt_ref,
        }
        if case == "partial":
            del facts[f"{fact_prefix}_manifest_hash"]
        elif case == "forged":
            facts[f"{fact_prefix}_content_hash"] = "f" * 64
        runtime.owners.human_collaboration.respond_to_human_request(
            str(request["request_ref"]),
            decision="provided",
            facts=facts,
            note="Use this exact accepted asset.",
            idempotency_key=f"root-human-asset-response-{case}",
        )

        current = owner.query_human_request(str(request["request_ref"]))
        assert current is not None
        if case == "exact":
            assert current["status"] == "satisfied"
            assert current["direct_waiters"][0]["status"] == "consumed"
            assert current["evaluation"]["accepted_evidence_refs"] == [
                asset.receipt.receipt_ref
            ]
        else:
            assert current["status"] == "open"
            assert current["evaluation"]["decision"] == "needs_input"
            assert current["direct_waiters"][0]["status"] == "blocked"
            assert owner.query_managed_run(str(binding["task_ref"]))[
                "status"
            ] == "suspended"
    finally:
        runtime.close()


@pytest.mark.parametrize(
    "case",
    ("exact", "decision_only", "confirmation_receipt", "wrong_scope_receipt"),
)
def test_root_human_request_capability_proof_requires_exact_independent_receipt(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / f"root-human-capability-{case}")
    )
    owner = runtime.owners.agent_runtime
    human = runtime.owners.human_collaboration
    _seed_root_human_request_task(runtime)
    _allow_current_root_human_request_scope(monkeypatch, owner)
    binding = _root_effect_binding()
    effect_id = "root-human-capability"
    required_authorization = {
        "capability": "external_publish",
        "scope": {
            "schema_ref": "meta-research/root-human-request-capability-scope/v1",
            "human_request_effect_id": effect_id,
            "quest_ref": binding["quest_ref"],
            "task_ref": binding["task_ref"],
            "root_session_ref": binding["root_session_ref"],
            "operation_id": binding["operation_id"],
            "attempt_ref": binding["attempt_ref"],
            "generation": binding["generation"],
            "destination": "https://publisher.example.invalid/exact-target",
            "duration": "single_effect",
            "exclusions": ["credential_export", "unrelated_targets"],
        },
    }
    confirmation, authorization = _grant_capability_authorization(
        human,
        scope_ref=f"quest:{binding['quest_ref']}",
        requirement=required_authorization,
        key=f"root-human-capability-{case}",
    )
    wrong_authorization = None
    if case == "wrong_scope_receipt":
        wrong_requirement = {
            **required_authorization,
            "scope": {
                **required_authorization["scope"],
                "destination": "https://publisher.example.invalid/other-target",
            },
        }
        _wrong_confirmation, wrong_authorization = _grant_capability_authorization(
            human,
            scope_ref=f"quest:{binding['quest_ref']}",
            requirement=wrong_requirement,
            key="root-human-capability-wrong-scope",
        )
    target = {
        "schema_ref": "meta-research/root-agent-human-request-target/v1",
        "root": {
            "run_kind": "deepfetch",
            "run_ref": binding["task_ref"],
            "attempt_ref": binding["attempt_ref"],
            "root_session_ref": binding["root_session_ref"],
            "fence_ref": binding["fence_ref"],
            "waiter_generation": binding["generation"],
        },
        "condition": {"capability": "external_publish"},
    }
    try:
        request = owner.open_human_request_effect(
            effect_key=f"mcp-effect:root-human-capability-{case}",
            effect_id=effect_id,
            operation_binding=binding,
            predecessor_request_ref=None,
            request_kind="capability_authorization",
            obligation="Authorize this exact external publish capability.",
            business_purpose="Resume only this exact blocked Root operation.",
            target_assertion=target,
            acceptance_conditions=("An exact independent grant is current.",),
            direct_waiter={
                "waiter_ref": f"root_run:{binding['task_ref']}",
                "generation": binding["generation"],
                "target_assertion": target,
                "wait_scope": "local",
                "other_blockers": [],
            },
            quest_ref=str(binding["quest_ref"]),
            required_authorization=required_authorization,
        )
        if case == "exact":
            facts = {
                "authorization_receipt_ref": authorization["receipt_ref"]
            }
        elif case == "decision_only":
            facts = {"authorization_decision": "granted"}
        elif case == "confirmation_receipt":
            facts = {
                "authorization_receipt_ref": confirmation[
                    "confirmation_receipt"
                ]["receipt_ref"]
            }
        else:
            assert wrong_authorization is not None
            facts = {
                "authorization_receipt_ref": wrong_authorization["receipt_ref"]
            }
        human.respond_to_human_request(
            str(request["request_ref"]),
            decision="provided",
            facts=facts,
            note="Use only the exact independently committed authorization.",
            idempotency_key=f"root-human-capability-response-{case}",
        )

        current = owner.query_human_request(str(request["request_ref"]))
        assert current is not None
        if case == "exact":
            assert current["status"] == "satisfied"
            assert current["direct_waiters"][0]["status"] == "consumed"
            assert current["evaluation"]["accepted_evidence_refs"] == [
                authorization["receipt_ref"]
            ]
        else:
            assert current["status"] == "open"
            assert current["evaluation"]["decision"] == "needs_input"
            assert current["direct_waiters"][0]["status"] == "blocked"
            assert owner.query_managed_run(str(binding["task_ref"]))[
                "status"
            ] == "suspended"
    finally:
        runtime.close()


@pytest.mark.parametrize(
    "case",
    ("exact", "missing_session", "not_ready", "slot_held"),
)
def test_root_human_request_institution_preflight_proof_is_owner_derived(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / f"root-human-institution-{case}")
    )
    owner = runtime.owners.agent_runtime
    _seed_root_human_request_task(runtime)
    _allow_current_root_human_request_scope(monkeypatch, owner)
    session_ref = "acquisition_session_root_human_request_1"
    generation = 7
    session = SimpleNamespace(
        session_ref=session_ref,
        status="waiting_user" if case == "not_ready" else "ready",
        slot_held=case == "slot_held",
        mode="oa_then_institution",
        browser_context_ref="browser_context_root_human_request_1",
        preflight_generation=generation,
    )

    def query_session(**values: object):
        assert values == {"quest_ref": "quest_root_human_request_1"}
        return None if case == "missing_session" else session

    monkeypatch.setattr(owner, "query_acquisition_session", query_session)
    try:
        request = _open_root_human_request_effect(
            owner,
            effect_key=f"mcp-effect:root-human-institution-{case}",
            effect_id=f"root-human-institution-{case}",
        )
        preflight_ref = agent_runtime_module._acquisition_preflight_runtime_effect(
            session_ref=session_ref,
            generation=generation,
        ).operation_ref
        runtime.owners.human_collaboration.respond_to_human_request(
            request["request_ref"],
            decision="provided",
            facts={"route": "institutional_browser_reconnected"},
            note="I reconnected the institution browser.",
            idempotency_key=f"root-human-institution-{case}-response",
        )

        current = owner.query_human_request(request["request_ref"])
        assert current is not None
        if case == "exact":
            assert current["status"] == "satisfied"
            assert current["evaluation"]["accepted_evidence_refs"] == [
                preflight_ref
            ]
            assert current["direct_waiters"][0]["status"] == "consumed"
            assert owner.query_managed_run(
                str(request["open_effect"]["operation_binding"]["task_ref"])
            )["status"] == "running"
        else:
            assert current["status"] == "open"
            assert current["evaluation"]["decision"] == "needs_input"
            assert current["disposition"] is None
            assert current["direct_waiters"][0]["status"] == "blocked"
    finally:
        runtime.close()


def test_root_human_request_successor_has_new_effect_waiter_and_lineage(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "root-human-request-successor")
    )
    owner = runtime.owners.agent_runtime
    _seed_root_human_request_task(runtime)
    _allow_current_root_human_request_scope(monkeypatch, owner)
    try:
        predecessor = _open_root_human_request_effect(
            owner,
            effect_key="mcp-effect:root-human-predecessor",
            effect_id="root-human-predecessor",
        )
        runtime.owners.human_collaboration.respond_to_human_request(
            predecessor["request_ref"],
            decision="deferred",
            facts={},
            note="Continue without this route for now.",
            idempotency_key="root-human-predecessor-response",
        )
        successor = _open_root_human_request_effect(
            owner,
            effect_key="mcp-effect:root-human-successor",
            effect_id="root-human-successor",
            generation=4,
            predecessor_request_ref=str(predecessor["request_ref"]),
        )
        assert owner.query_managed_run(
            str(successor["open_effect"]["operation_binding"]["task_ref"])
        )["status"] == "suspended"

        old = owner.query_human_request(predecessor["request_ref"])
        assert old is not None
        assert successor["predecessor_request_ref"] == predecessor["request_ref"]
        assert successor["lineage"] == {
            "predecessor_request_ref": predecessor["request_ref"],
            "successor_request_ref": None,
        }
        assert old["successor_request_ref"] == successor["request_ref"]
        assert old["lineage"]["successor_request_ref"] == successor[
            "request_ref"
        ]
        assert successor["request_ref"] != predecessor["request_ref"]
        assert successor["open_receipt"] != predecessor["open_receipt"]
        assert successor["generation"] == 4
        assert successor["direct_waiters"][0]["generation"] == 4
        stale = owner.validate_human_request_waiter(
            successor["request_ref"],
            waiter_ref=predecessor["direct_waiters"][0]["waiter_ref"],
            generation=3,
            target_assertion=predecessor["target_assertion"],
            other_blockers=(),
            idempotency_key="old-receipt-cannot-release-successor",
        )
        assert stale["status"] == "blocked"
        assert stale["reason"] == {"code": "satisfied_disposition_required"}
    finally:
        runtime.close()
