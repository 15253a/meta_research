from __future__ import annotations

import time

import pytest
from sqlalchemy import text

from meta_research.composition import build_production_runtime
from meta_research.owners.common import OwnerConflict
from meta_research.owners import human_requests as human_requests_module
from meta_research.paths import prepare_data_root
from meta_research.quest_drafting import (
    IntentTurnResult,
    ProposalDraftResult,
)


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


def test_successor_terminal_recurrence_and_secret_exclusion(tmp_path) -> None:
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

        with pytest.raises(OwnerConflict, match="human_response_secret_forbidden"):
            runtime.owners.human_collaboration.respond_to_human_request(
                successor["request_ref"],
                decision="provided",
                facts={"cookie": "session=secret"},
                note="",
                idempotency_key="secret-cookie",
            )
        for index, (facts, note) in enumerate(
            (
                ({"token": "ghp_examplecredential"}, ""),
                ({"client_secret": "client-secret-value"}, ""),
                ({"private_key": "-----BEGIN PRIVATE KEY-----"}, ""),
                ({"aws_secret_access_key": "example-secret-access-key"}, ""),
                ({}, "token: ghp_examplecredential"),
                ({}, "My password is hunter2"),
                ({}, "Here is the OTP 123456"),
                ({}, "AWS secret access key is example-secret-access-key"),
                ({}, "Cookie sessionid abcdef123456"),
                ({}, "-----begin rsa private key-----"),
                (
                    {},
                    "https://blob.example.test/file?sv=1&se=2&sp=r&sig=abcdef0123456789",
                ),
                (
                    {},
                    "https://s3.example.test/file?X-Amz-Algorithm=v4&X-Amz-Signature=abcdef0123456789",
                ),
                ({}, "https://alice:hunter2@example.test/private"),
                ({}, "postgresql://alice:hunter2@db.example.test/research"),
                ({}, "sessionid=abcdef0123456789"),
            )
        ):
            with pytest.raises(
                OwnerConflict, match="human_response_secret_forbidden"
            ):
                runtime.owners.human_collaboration.respond_to_human_request(
                    successor["request_ref"],
                    decision="provided",
                    facts=facts,
                    note=note,
                    idempotency_key=f"secret-expanded-{index}",
                )

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


def test_owner_human_request_facts_reject_credentials_before_persistence(
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
                target_assertion={"token": "ghp_examplecredential"},
                acceptance_conditions=("The exact route is ready.",),
                direct_waiter=_waiter("secret-target-waiter"),
                idempotency_key="secret-target-open",
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
        assert owner.query_human_request(request["request_ref"])["responses"] == []
        response = runtime.owners.human_collaboration.respond_to_human_request(
            request["request_ref"],
            decision="provided",
            facts={"route_status": "ready"},
            note="The exact route is ready.",
            idempotency_key="secret-evaluation-response",
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
