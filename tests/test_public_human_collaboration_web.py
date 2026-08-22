from __future__ import annotations

import time
from pathlib import Path
from urllib.parse import quote

from fastapi.testclient import TestClient

from meta_research.composition import build_production_runtime
from meta_research.paths import prepare_data_root
from meta_research.quest_drafting import IntentTurnResult, ProposalDraftResult
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
            adapter_kind="deterministic_web_test",
        )

    def reply(self, request) -> IntentTurnResult:
        return IntentTurnResult(
            reply=f"Companion reply: {request.message}",
            native_session_ref=request.native_session_ref or "native_web_companion",
            adapter_kind="deterministic_web_test",
        )


def _runtime(path: Path):
    provider = _DeterministicDraftingProvider()
    return build_production_runtime(
        prepare_data_root(path),
        proposal_drafter=provider,
        intent_drafting_provider=provider,
    )


def _authenticated_client(runtime) -> tuple[TestClient, dict[str, str]]:
    base_url = "http://testserver"
    client = TestClient(
        create_app(runtime, base_url=base_url, control_key="control-secret"),
        base_url=base_url,
    )
    bootstrap = runtime.authentication.issue_bootstrap_token()
    response = client.post(
        "/auth/bootstrap",
        headers={"Origin": base_url},
        json={"token": bootstrap},
    )
    assert response.status_code == 200
    return client, {
        "Origin": base_url,
        "X-CSRF-Token": response.json()["csrf_token"],
    }


def _write_headers(auth: dict[str, str], key: str) -> dict[str, str]:
    return {**auth, "Idempotency-Key": key}


def _open_request(
    owner,
    *,
    request_kind: str,
    waiter_ref: str,
    wait_scope: str,
    other_blockers: tuple[str, ...],
    idempotency_key: str,
) -> dict[str, object]:
    target_assertion = {
        "target_ref": f"target:{waiter_ref}",
        "generation": 1,
    }
    return owner.open_human_request(
        request_kind=request_kind,
        obligation=f"Provide the exact human input for {waiter_ref}.",
        business_purpose=f"Resume only {waiter_ref} after Owner evaluation.",
        target_assertion=target_assertion,
        acceptance_conditions=("The issuing Owner accepts exact evidence.",),
        direct_waiter={
            "waiter_ref": waiter_ref,
            "generation": 1,
            "target_assertion": target_assertion,
            "wait_scope": wait_scope,
            "other_blockers": list(other_blockers),
        },
        quest_ref="quest_projection",
        required_authorization=(
            {
                "capability": "external_publish",
                "scope": {"quest_ref": "quest_projection"},
            }
            if request_kind == "capability_authorization"
            else None
        ),
        idempotency_key=idempotency_key,
    )


def _post_response(
    client: TestClient,
    auth: dict[str, str],
    request_ref: str,
    *,
    key: str,
    facts: dict[str, object],
) -> dict[str, object]:
    response = client.post(
        f"/api/v1/human-requests/{quote(request_ref, safe='')}/responses",
        headers=_write_headers(auth, key),
        json={
            "decision": "provided",
            "facts": facts,
            "note": "Non-secret evidence for Owner evaluation.",
        },
    )
    assert response.status_code == 201, response.json()
    return response.json()


def _satisfy_request(runtime, owner, request_ref: str, key: str) -> None:
    evidence_ref = f"accepted-observation:{key}"
    response = runtime.owners.human_collaboration.respond_to_human_request(
        request_ref,
        decision="provided",
        facts={"observation_ref": evidence_ref},
        note="The exact non-secret observation is available for Owner evaluation.",
        idempotency_key=f"{key}-response",
    )
    terminal = owner.evaluate_human_request(
        request_ref,
        response_refs=(response["response_ref"],),
        decision="satisfied",
        reason_code="exact_observation_accepted",
        accepted_evidence_refs=(evidence_ref,),
        idempotency_key=f"{key}-evaluation",
    )
    assert terminal["status"] == "satisfied"
    assert terminal["current"] is True
    assert terminal["disposition"]["decision"] == "satisfied"


def test_snapshot_aggregates_four_owner_requests_without_auto_resume(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path / "human-request-projection")
    owners = runtime.owners
    requests = {
        "agent_runtime": _open_request(
            owners.agent_runtime,
            request_kind="library_reconnect",
            waiter_ref="deepfetch_run_1",
            wait_scope="local",
            other_blockers=(),
            idempotency_key="web-open-library",
        ),
        "research_memory": _open_request(
            owners.research_memory,
            request_kind="external_material_api_access",
            waiter_ref="asset_intake_1",
            wait_scope="local",
            other_blockers=("provider_unavailable",),
            idempotency_key="web-open-external",
        ),
        "research_graph": _open_request(
            owners.research_graph,
            request_kind="offline_action",
            waiter_ref="protocol_run_1",
            wait_scope="quest",
            other_blockers=(),
            idempotency_key="web-open-offline",
        ),
        "advancement_engine": _open_request(
            owners.advancement_engine,
            request_kind="capability_authorization",
            waiter_ref="stage_transition_1",
            wait_scope="local",
            other_blockers=("policy_gate",),
            idempotency_key="web-open-authorization",
        ),
    }
    client, auth = _authenticated_client(runtime)
    try:
        with client:
            response = _post_response(
                client,
                auth,
                requests["agent_runtime"]["request_ref"],
                key="web-response-library",
                facts={"route": "institutional_browser", "profile_ref": "opaque:lab"},
            )
            owner_view = owners.agent_runtime.query_human_request(
                requests["agent_runtime"]["request_ref"]
            )
            assert owner_view is not None
            assert owner_view["responses"] == [response]
            assert owner_view["evaluation"] is None
            assert owner_view["disposition"] is None
            assert owner_view["direct_waiters"][0]["status"] == "blocked"

            memory_response = _post_response(
                client,
                auth,
                requests["research_memory"]["request_ref"],
                key="web-response-external",
                facts={"application_ref": "application_1", "status": "submitted"},
            )
            owners.research_memory.evaluate_human_request(
                requests["research_memory"]["request_ref"],
                response_refs=(memory_response["response_ref"],),
                decision="needs_input",
                reason_code="external_access_not_yet_verified",
                accepted_evidence_refs=(),
                idempotency_key="web-evaluate-external",
            )

            graph_response = _post_response(
                client,
                auth,
                requests["research_graph"]["request_ref"],
                key="web-response-offline",
                facts={"observation_ref": "offline_observation_1"},
            )
            owners.research_graph.evaluate_human_request(
                requests["research_graph"]["request_ref"],
                response_refs=(graph_response["response_ref"],),
                decision="satisfied",
                reason_code="offline_observation_accepted",
                accepted_evidence_refs=("offline_observation_1",),
                idempotency_key="web-evaluate-offline",
            )

            snapshot_response = client.get("/api/v1/snapshot")
            assert snapshot_response.status_code == 200
            collaboration = snapshot_response.json()["human_collaboration"]
            request_projection = collaboration["human_requests"]
            assert request_projection["status"] == "ready"
            # The quest-scoped request is already satisfied, so only the two
            # still-open local waiters contribute to the aggregate wait state.
            assert request_projection["waiting"]["scope"] == "local"
            assert request_projection["waiting"][
                "safe_meaningful_runnable_exists"
            ] is False
            assert request_projection["waiting"]["safe_runnable_basis"] == []
            assert set(request_projection["waiting"]["other_blockers"]) == {
                "policy_gate",
                "provider_unavailable",
            }

            by_issuer = {
                item["issuer"]: item for item in request_projection["items"]
            }
            assert set(by_issuer) == {
                "agent_runtime",
                "research_memory",
                "research_graph",
                "advancement_engine",
            }
            assert by_issuer["agent_runtime"]["responses"] == [response]
            assert by_issuer["agent_runtime"]["evaluation"] is None
            assert by_issuer["agent_runtime"]["disposition"] is None
            assert by_issuer["agent_runtime"]["direct_waiters"][0][
                "wait_scope"
            ] == "local"
            assert by_issuer["research_memory"]["evaluation"]["decision"] == (
                "needs_input"
            )
            assert by_issuer["research_memory"]["disposition"] is None
            assert by_issuer["research_graph"]["evaluation"]["decision"] == (
                "satisfied"
            )
            assert by_issuer["research_graph"]["disposition"]["decision"] == (
                "satisfied"
            )
            assert by_issuer["research_graph"]["direct_waiters"][0][
                "status"
            ] == "blocked"
            assert by_issuer["advancement_engine"]["direct_waiters"][0][
                "other_blockers"
            ] == ["policy_gate"]
    finally:
        client.close()
        runtime.close()


def test_snapshot_survives_more_than_101_current_terminal_human_requests(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path / "bounded-terminal-human-request-projection")
    owner = runtime.owners.agent_runtime
    try:
        original = _open_request(
            owner,
            request_kind="offline_action",
            waiter_ref="terminal-revision-original",
            wait_scope="local",
            other_blockers=(),
            idempotency_key="terminal-revision-open",
        )
        successor_target = {
            "target_ref": "target:terminal-revision-successor",
            "generation": 2,
        }
        successor = owner.revise_human_request(
            original["request_ref"],
            expected_revision=1,
            obligation="Provide the revised exact human input for the successor.",
            target_assertion=successor_target,
            acceptance_conditions=(
                "The issuing Owner accepts the revised exact evidence.",
            ),
            direct_waiters=(
                {
                    "waiter_ref": "terminal-revision-successor",
                    "generation": 2,
                    "target_assertion": successor_target,
                    "wait_scope": "local",
                    "other_blockers": [],
                },
            ),
            idempotency_key="terminal-revision-successor",
        )
        _satisfy_request(
            runtime,
            owner,
            successor["request_ref"],
            "terminal-revision-successor",
        )
        expected_current_refs = {successor["request_ref"]}

        for ordinal in range(101):
            key = f"terminal-current-{ordinal:03d}"
            request = _open_request(
                owner,
                request_kind="offline_action",
                waiter_ref=key,
                wait_scope="local",
                other_blockers=(),
                idempotency_key=f"{key}-open",
            )
            _satisfy_request(runtime, owner, request["request_ref"], key)
            expected_current_refs.add(request["request_ref"])

        assert len(expected_current_refs) == 102
        current_requests = owner.query_human_requests()
        assert len(current_requests) == 102
        assert {item["request_ref"] for item in current_requests} == (
            expected_current_refs
        )
        assert {item["status"] for item in current_requests} == {"satisfied"}
        assert {item["current"] for item in current_requests} == {True}

        request_history = owner.query_human_requests(include_history=True)
        assert len(request_history) == 103
        historical_only = {
            item["request_ref"]
            for item in request_history
            if item["request_ref"] not in expected_current_refs
        }
        assert historical_only == {original["request_ref"]}
        historical_original = next(
            item
            for item in request_history
            if item["request_ref"] == original["request_ref"]
        )
        assert historical_original["status"] == "superseded"
        assert historical_original["current"] is False

        snapshot = runtime.projection.query_snapshot()
        projection = snapshot["human_collaboration"]["human_requests"]
        assert projection["status"] == "ready"
        assert len(projection["items"]) == 102
        assert {item["request_ref"] for item in projection["items"]} == (
            expected_current_refs
        )
        assert original["request_ref"] not in {
            item["request_ref"] for item in projection["items"]
        }
        assert projection["waiting"] == {
            "scope": "none",
            "safe_meaningful_runnable_exists": False,
            "safe_runnable_basis": [],
            "other_blockers": [],
        }
    finally:
        runtime.close()


def test_companion_and_guidance_web_facts_are_durable_in_snapshot(
    tmp_path: Path,
) -> None:
    data_path = tmp_path / "companion-web"
    runtime = _runtime(data_path)
    client, auth = _authenticated_client(runtime)
    scope_ref = "workspace"
    try:
        with client:
            denied = client.post(
                "/api/v1/companion/messages",
                headers={
                    "Origin": "http://testserver",
                    "Idempotency-Key": "companion-missing-csrf",
                },
                json={"scope_ref": scope_ref, "message": "This must be denied."},
            )
            assert denied.status_code == 403
            assert denied.json()["detail"]["code"] == "csrf_invalid"

            queued_response = client.post(
                "/api/v1/companion/messages",
                headers=_write_headers(auth, "companion-message-web-1"),
                json={
                    "scope_ref": scope_ref,
                    "message": "Why is this only a local wait?",
                },
            )
            assert queued_response.status_code == 202, queued_response.json()
            queued = queued_response.json()
            assert queued["interaction_kind"] == "conversation"
            assert queued["assistant_status"] == "queued"

            proposal_response = client.post(
                "/api/v1/human-collaboration/agent-proposals",
                headers=_write_headers(auth, "agent-proposal-web-1"),
                json={
                    "scope_ref": scope_ref,
                    "proposal": {
                        "proposal_kind": "narrow_scope",
                        "text": "Start with public literature.",
                    },
                },
            )
            assert proposal_response.status_code == 201, proposal_response.json()
            assert proposal_response.json()["status"] == "proposed"
            assert proposal_response.json()["authoritative_effect"] is False

            constraint_response = client.post(
                "/api/v1/human-collaboration/soft-constraints",
                headers=_write_headers(auth, "soft-constraint-web-1"),
                json={
                    "scope_ref": scope_ref,
                    "guidance": {
                        "text": "Prefer public literature for the first pass.",
                        "applies_to": ["idea"],
                    },
                },
            )
            assert constraint_response.status_code == 201, constraint_response.json()
            constraint = constraint_response.json()
            assert constraint["status"] == "active"

            companion: dict[str, object] = {}
            deadline = time.monotonic() + 4
            while time.monotonic() < deadline:
                snapshot = client.get("/api/v1/snapshot").json()
                companion = snapshot["human_collaboration"]["companion"]
                assistant_messages = [
                    message
                    for message in companion["messages"]
                    if message["role"] == "assistant"
                ]
                if assistant_messages and assistant_messages[-1]["status"] == (
                    "completed"
                ):
                    break
                time.sleep(0.05)
            assert companion["scope_ref"] == scope_ref
            assert any(
                message["role"] == "user"
                and message["content"] == "Why is this only a local wait?"
                for message in companion["messages"]
            )
            assert any(
                message["role"] == "assistant"
                and message["status"] == "completed"
                and message["content"].startswith("Companion reply:")
                for message in companion["messages"]
            )
            assert companion["agent_proposals"] == [proposal_response.json()]
            assert companion["soft_constraints"] == [constraint]

            withdrawn_response = client.post(
                "/api/v1/human-collaboration/soft-constraints/"
                f"{quote(constraint['constraint_ref'], safe='')}/withdrawals",
                headers=_write_headers(auth, "soft-constraint-withdraw-web-1"),
                json={"expected_revision": constraint["revision"]},
            )
            assert withdrawn_response.status_code == 200
            assert withdrawn_response.json()["status"] == "withdrawn"
    finally:
        client.close()
        runtime.close()

    restarted = _runtime(data_path)
    restarted_client, _restarted_auth = _authenticated_client(restarted)
    try:
        with restarted_client:
            companion = restarted_client.get("/api/v1/snapshot").json()[
                "human_collaboration"
            ]["companion"]
            assert companion["scope_ref"] == scope_ref
            assert any(
                message["role"] == "assistant"
                and message["status"] == "completed"
                for message in companion["messages"]
            )
            assert companion["agent_proposals"] == [proposal_response.json()]
            assert companion["soft_constraints"][0]["status"] == "withdrawn"
    finally:
        restarted_client.close()
        restarted.close()


def test_command_web_requires_exact_preview_then_separate_authorization(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path / "command-web")
    client, auth = _authenticated_client(runtime)
    # No Quest exists in this vertical slice, so the public Snapshot's exact
    # collaboration scope is the workspace.
    scope_ref = "workspace"
    command = {
        "command_kind": "capability_authorization",
        "payload": {
            "capability": "external_publish",
            "decision": "granted",
            "scope": {
                "destination": "https://example.invalid/publication",
                "asset_ref": "asset_publication_1",
            },
        },
    }
    try:
        with client:
            created_response = client.post(
                "/api/v1/human-collaboration/commands",
                headers=_write_headers(auth, "command-create-web-1"),
                json={"scope_ref": scope_ref, "command": command},
            )
            assert created_response.status_code == 201, created_response.json()
            created = created_response.json()
            assert created["draft_revision"] == 1
            assert created["status"] == "draft"

            replay = client.post(
                "/api/v1/human-collaboration/commands",
                headers=_write_headers(auth, "command-create-web-1"),
                json={"scope_ref": scope_ref, "command": command},
            )
            assert replay.status_code == 201
            assert replay.json()["intent_id"] == created["intent_id"]

            preview_response = client.post(
                "/api/v1/human-collaboration/commands/"
                f"{quote(created['intent_id'], safe='')}/previews",
                headers=_write_headers(auth, "command-preview-web-1"),
                json={
                    "draft_revision": created["draft_revision"],
                    "draft_hash": created["draft_hash"],
                },
            )
            assert preview_response.status_code == 201, preview_response.json()
            previewed = preview_response.json()
            old_preview = previewed["impact_preview"]
            assert old_preview["status"] == "current"
            assert old_preview["draft_revision"] == created["draft_revision"]

            revised_command = {
                **command,
                "payload": {
                    **command["payload"],
                    "scope": {
                        **command["payload"]["scope"],
                        "asset_ref": "asset_publication_2",
                    },
                },
            }
            revision_response = client.post(
                "/api/v1/human-collaboration/commands/"
                f"{quote(created['intent_id'], safe='')}/revisions",
                headers=_write_headers(auth, "command-revise-web-1"),
                json={
                    "expected_revision": created["draft_revision"],
                    "command": revised_command,
                },
            )
            assert revision_response.status_code == 201, revision_response.json()
            revised = revision_response.json()
            assert revised["draft_revision"] == 2
            assert revised["impact_preview"]["status"] == "stale"

            stale_confirmation = client.post(
                "/api/v1/human-collaboration/commands/"
                f"{quote(created['intent_id'], safe='')}/confirmations",
                headers=_write_headers(auth, "command-confirm-stale-web-1"),
                json={
                    "draft_revision": created["draft_revision"],
                    "draft_hash": created["draft_hash"],
                    "preview_ref": old_preview["preview_ref"],
                    "preview_hash": old_preview["preview_hash"],
                },
            )
            assert stale_confirmation.status_code == 409
            assert stale_confirmation.json()["detail"]["code"] == (
                "command_preview_stale"
            )

            refreshed_response = client.post(
                "/api/v1/human-collaboration/commands/"
                f"{quote(created['intent_id'], safe='')}/previews",
                headers=_write_headers(auth, "command-preview-web-2"),
                json={
                    "draft_revision": revised["draft_revision"],
                    "draft_hash": revised["draft_hash"],
                },
            )
            assert refreshed_response.status_code == 201
            refreshed = refreshed_response.json()["impact_preview"]
            confirmation_response = client.post(
                "/api/v1/human-collaboration/commands/"
                f"{quote(created['intent_id'], safe='')}/confirmations",
                headers=_write_headers(auth, "command-confirm-web-1"),
                json={
                    "draft_revision": revised["draft_revision"],
                    "draft_hash": revised["draft_hash"],
                    "preview_ref": refreshed["preview_ref"],
                    "preview_hash": refreshed["preview_hash"],
                },
            )
            assert confirmation_response.status_code == 201
            confirmed = confirmation_response.json()
            confirmation_receipt = confirmed["confirmation_receipt"]
            assert confirmation_receipt["status"] == "accepted"
            assert confirmed.get("authorization") is None

            authorization_response = client.post(
                "/api/v1/human-collaboration/commands/"
                f"{quote(created['intent_id'], safe='')}/authorizations",
                headers=_write_headers(auth, "authorization-web-1"),
                json={
                    "capability": revised_command["payload"]["capability"],
                    "decision": revised_command["payload"]["decision"],
                    "scope": revised_command["payload"]["scope"],
                    "confirmation_receipt_ref": confirmation_receipt["receipt_ref"],
                },
            )
            assert authorization_response.status_code == 201
            authorization = authorization_response.json()
            assert authorization["decision"] == "granted"
            assert authorization["receipt_ref"] != confirmation_receipt[
                "receipt_ref"
            ]

            commands = client.get("/api/v1/snapshot").json()[
                "human_collaboration"
            ]["commands"]
            assert commands["status"] == "ready"
            assert commands["items"][0]["confirmation_receipt"]["receipt_ref"] == (
                confirmation_receipt["receipt_ref"]
            )
            assert commands["authorizations"][0]["receipt_ref"] == (
                authorization["receipt_ref"]
            )
    finally:
        client.close()
        runtime.close()
