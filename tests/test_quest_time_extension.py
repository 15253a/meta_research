"""Quest time approval through the existing HumanRequest/Owner public seams."""
from __future__ import annotations

from pathlib import Path
from urllib.parse import quote
from itertools import count
import uuid

import pytest
from fastapi.testclient import TestClient

from meta_research.owners.common import OwnerConflict
from meta_research.semantic_mcp import ROOT_AGENT_HUMAN_REQUEST_OPERATION_IDS
from meta_research.web import create_app
from test_root_human_request_lifecycle import (
    _bundle_root_channel, _initialized_child, _runtime as _root_runtime,
    _structured, _tool_call,
)

from test_public_human_collaboration_web import _authenticated_client, _confirm_direct_quest, _runtime


def _open_extension(runtime, quest, revision, key="extension"):
    target = {"condition": {"time_extension": {
        "budget_revision": revision, "requested_additional_seconds": 3600,
    }}}
    return runtime.owners.agent_runtime.open_human_request(
        request_kind="research_time_extension", quest_ref=quest,
        obligation=f"Allow time to validate the negative result for {key}.",
        business_purpose="The completed pilot leaves a one-hour verification gap.",
        target_assertion=target,
        acceptance_conditions=("The human explicitly approves a time allowance.",),
        direct_waiter={"waiter_ref": key + "-waiter", "generation": 1,
                       "target_assertion": target, "wait_scope": "local", "other_blockers": []},
        idempotency_key=key + "-open",
    )


def test_research_time_extension_is_a_formal_human_request(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path / "extension")
    try:
        quest = _confirm_direct_quest(runtime)["quest_ref"]
        before = runtime.owners.advancement_engine.query_research_time(quest)
        request = _open_extension(runtime, quest, before["budget_revision"])
        readback = runtime.owners.human_collaboration.query_human_request(request["request_ref"])
        assert readback["kind"] == "research_time_extension"
        assert readback["status"] == "open"
        assert readback["quest_ref"] == quest
    finally:
        runtime.close()


def test_original_root_requires_explicit_approval_then_consumes_new_allowance(tmp_path, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr("time.time", lambda: now[0])
    runtime = _root_runtime(tmp_path / "root-extension")
    try:
        runtime.owners.advancement_engine.start_research_time_service()
        run, channel = _bundle_root_channel(runtime)
        ar = runtime.owners.agent_runtime
        human = runtime.owners.human_collaboration
        quest = ar.query_managed_run(run.run_ref)["quest_ref"]
        now[0] = 1060.0
        budget = runtime.owners.advancement_engine.query_research_time(quest)
        with TestClient(create_app(runtime, base_url="http://testserver", control_key="control")) as client:
            headers = _initialized_child(client, channel.connection.token)
            arguments = {
                "effect_id": "root-extension", "request_kind": "research_time_extension",
                "obligation": "Allow one extra hour for verification.",
                "business_purpose": "The negative pilot needs uncertainty checks before handoff.",
                "condition": {"impact": "Without more time, hand off the pilot and its uncertainty.",
                    "safe_response": "Explicitly approve an allowance or decline.",
                    "time_extension": {"budget_revision": budget["budget_revision"],
                                       "requested_additional_seconds": 3600}},
                "acceptance_conditions": ["The approved allowance is applied to this Quest."],
            }
            opened = _structured(_tool_call(client, headers,
                operation_id=ROOT_AGENT_HUMAN_REQUEST_OPERATION_IDS[0], arguments=arguments, request_id=10))
            request_ref = opened["request_ref"]
            original = ar.query_human_request(request_ref)
            assert original["target_assertion"]["condition"]["time_extension"]["snapshot"]["budget_revision"] == budget["budget_revision"]
            assert ar.query_managed_run(run.run_ref)["status"] == "suspended"
            now[0] = 1180.0
            assert runtime.owners.advancement_engine.query_research_time(quest)["used_seconds"] == 180.0
            # Equal timestamps must follow actual submission order even when
            # random response identities sort in the opposite direction.
            identities = count(2**128 - 1, -1)
            monkeypatch.setattr(uuid, "uuid4", lambda: uuid.UUID(int=next(identities)))
            human.respond_to_human_request(request_ref, decision="provided", facts={},
                note="I am considering it.", idempotency_key="ordinary-answer")
            undecided = ar.query_human_request(request_ref)
            assert undecided["status"] == "open"
            assert undecided["evaluation"]["decision"] == "needs_input"
            assert runtime.owners.advancement_engine.query_research_time(quest)["budget_revision"] == budget["budget_revision"]
            auth = client.post("/auth/bootstrap", headers={"Origin": "http://testserver"},
                               json={"token": runtime.authentication.issue_bootstrap_token()})
            assert auth.status_code == 200
            response_headers = {"Origin": "http://testserver", "X-CSRF-Token": auth.json()["csrf_token"],
                                "Idempotency-Key": "root-approved"}
            response_body = {"decision": "provided", "facts": {"time_budget_decision": {
                "decision": "approve", "expected_revision": budget["budget_revision"],
                "additional_seconds": 3600}}, "note": "One extra hour approved."}
            posted = client.post(f"/api/v1/human-requests/{quote(request_ref, safe='')}/responses",
                                 headers=response_headers, json=response_body)
            assert posted.status_code == 201, posted.json()
            approved = posted.json()
            after = ar.query_human_request(request_ref)
            assert after["status"] == "satisfied"
            assert after["direct_waiters"][0]["status"] == "consumed"
            assert after["direct_waiters"][0]["resume_validation"]["consumption"]["work_ref"] == run.run_ref
            resumed = ar.query_managed_run(run.run_ref)
            assert resumed["status"] == "running"
            assert resumed["root_session_ref"] == run.root_session_ref
            after_budget = runtime.owners.advancement_engine.query_research_time(quest)
            assert after_budget["used_seconds"] == 180.0
            assert after_budget["approved_seconds"] == budget["approved_seconds"] + 3600
            reconciled = _structured(_tool_call(client, headers,
                operation_id=ROOT_AGENT_HUMAN_REQUEST_OPERATION_IDS[1],
                arguments={"effect_id": "root-extension"}, request_id=11))
            assert reconciled["resolution"]["time_budget_approval"] == approved["time_budget_approval"]
            retried = client.post(f"/api/v1/human-requests/{quote(request_ref, safe='')}/responses",
                                  headers=response_headers, json=response_body)
            assert retried.status_code == 201, retried.json()
            assert retried.json() == approved
            assert runtime.owners.advancement_engine.query_research_time(quest) == after_budget
    finally:
        runtime.close()


def test_human_can_confirm_remaining_allowance_after_unwitnessed_service_stop(tmp_path, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr("time.time", lambda: now[0])
    path = tmp_path / "corrected-extension"
    first = _runtime(path)
    second = None
    try:
        first.owners.advancement_engine.start_research_time_service()
        quest = _confirm_direct_quest(first)["quest_ref"]
        now[0] = 1060.0
        first.owners.advancement_engine.checkpoint_research_time_service()
        now[0] = 2000.0
        second = _runtime(path)
        ae = second.owners.advancement_engine
        ae.start_research_time_service()
        before = ae.query_research_time(quest)
        assert before["accuracy"] == "unknown"
        request = _open_extension(second, quest, before["budget_revision"])
        now[0] = 2010.0
        response = second.owners.human_collaboration.respond_to_human_request(
            request["request_ref"], decision="provided", facts={"time_budget_decision": {
                "decision": "approve", "expected_revision": before["budget_revision"],
                "total_seconds": 7200, "confirmed_remaining_seconds": 3600,
                "correction_reason": "Confirm one remaining hour after checking the unobserved interval.",
            }}, note="This is the confirmed remaining allowance.", idempotency_key="confirmed-remaining")
        after = ae.query_research_time(quest)
        assert after["confirmed_used_seconds"] == 70.0
        assert after["used_seconds"] == 3600.0
        assert after["remaining_seconds"] == 3600.0
        assert after["measurement_basis"] == "human_confirmation"
        assert after["accuracy"] == "exact"
        correction = response["time_budget_approval"]["time_accounting_correction"]
        assert correction["previous_unknown_intervals"] == before["unknown_intervals"]
        now[0] = 2020.0
        assert ae.query_research_time(quest)["remaining_seconds"] == 3590.0
    finally:
        if second is not None:
            second.close()
        first.close()


def test_explicit_extension_applies_once_without_erasing_waiting_time(tmp_path, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr("time.time", lambda: now[0])
    runtime = _runtime(tmp_path / "approved-extension")
    try:
        ae = runtime.owners.advancement_engine
        ae.start_research_time_service()
        quest = _confirm_direct_quest(runtime)["quest_ref"]
        now[0] = 1060.0
        before = ae.query_research_time(quest)
        request = _open_extension(runtime, quest, before["budget_revision"])
        now[0] = 1180.0
        facts = {"time_budget_decision": {"decision": "approve",
            "expected_revision": before["budget_revision"], "additional_seconds": 3600}}
        response = runtime.owners.human_collaboration.respond_to_human_request(
            request["request_ref"], decision="provided", facts=facts,
            note="Approve one more hour for verification.", idempotency_key="approve-hour",
        )
        after = ae.query_research_time(quest)
        assert after["approved_seconds"] == 608400.0
        assert after["used_seconds"] == 180.0
        assert after["remaining_seconds"] == 608220.0
        assert after["budget_revision"] != before["budget_revision"]
        replay = runtime.owners.human_collaboration.respond_to_human_request(
            request["request_ref"], decision="provided", facts=facts,
            note="Approve one more hour for verification.", idempotency_key="approve-hour",
        )
        assert replay == response
        assert ae.query_research_time(quest) == after
        assert response["time_budget_approval"]["response_ref"] == response["response_ref"]
        history = runtime.owners.human_collaboration.query_research_help_response(
            request["request_ref"], response["response_ref"], quest_ref=quest,
        )
        assert history["response"]["time_budget_approval"] == response["time_budget_approval"]
    finally:
        runtime.close()


def test_request_replay_keeps_original_observation_and_changed_submission_conflicts(tmp_path, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr("time.time", lambda: now[0])
    runtime = _runtime(tmp_path / "frozen-extension")
    try:
        ae = runtime.owners.advancement_engine
        ae.start_research_time_service()
        quest = _confirm_direct_quest(runtime)["quest_ref"]
        revision = ae.query_research_time(quest)["budget_revision"]
        original = _open_extension(runtime, quest, revision)
        now[0] = 1180.0
        replay = _open_extension(runtime, quest, revision)
        assert replay == original
        assert replay["target_assertion"]["condition"]["time_extension"]["snapshot"]["observed_at"] == 1000.0
        assert ae.query_research_time(quest)["used_seconds"] == 180.0
        with pytest.raises(OwnerConflict, match="idempotency_conflict"):
            _open_extension(runtime, quest, "changed-revision")
    finally:
        runtime.close()


def test_concurrent_old_budget_approval_has_no_response_or_budget_side_effect(tmp_path):
    runtime = _runtime(tmp_path / "stale-extension")
    try:
        ae = runtime.owners.advancement_engine
        quest = _confirm_direct_quest(runtime)["quest_ref"]
        revision = ae.query_research_time(quest)["budget_revision"]
        one = _open_extension(runtime, quest, revision, "one")
        two = _open_extension(runtime, quest, revision, "two")
        facts = {"time_budget_decision": {"decision": "approve", "expected_revision": revision,
                                         "additional_seconds": 3600}}
        runtime.owners.human_collaboration.respond_to_human_request(
            one["request_ref"], decision="provided", facts=facts, note="Approve.", idempotency_key="one-approved")
        with pytest.raises(OwnerConflict, match="quest_time_budget_revision_stale"):
            runtime.owners.human_collaboration.respond_to_human_request(
                two["request_ref"], decision="provided", facts=facts, note="Approve.", idempotency_key="two-approved")
        assert runtime.owners.agent_runtime.query_human_request(two["request_ref"])["responses"] == []
        assert ae.query_research_time(quest)["approved_seconds"] == 608400.0
    finally:
        runtime.close()


def test_revised_extension_freezes_new_observation_and_replays_it(tmp_path, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr("time.time", lambda: now[0])
    runtime = _runtime(tmp_path / "revised-extension")
    try:
        ae = runtime.owners.advancement_engine
        ae.start_research_time_service()
        quest = _confirm_direct_quest(runtime)["quest_ref"]
        budget_revision = ae.query_research_time(quest)["budget_revision"]
        original = _open_extension(runtime, quest, budget_revision)
        target = {"condition": {"time_extension": {
            "budget_revision": budget_revision, "requested_additional_seconds": 7200,
        }}}
        command = {
            "expected_revision": original["revision"],
            "obligation": "Allow two hours to complete the controlled verification.",
            "target_assertion": target,
            "acceptance_conditions": ("The human explicitly approves the revised allowance.",),
            "direct_waiters": ({"waiter_ref": "revised-waiter", "generation": 1,
                                "target_assertion": target, "wait_scope": "local", "other_blockers": []},),
            "idempotency_key": "revise-extension",
        }
        now[0] = 1060.0
        revised = runtime.owners.agent_runtime.revise_human_request(original["request_ref"], **command)
        extension = revised["target_assertion"]["condition"]["time_extension"]
        assert extension["snapshot"]["observed_at"] == 1060.0
        assert extension["snapshot"]["used_seconds"] == 60.0
        assert extension["requested_additional_seconds"] == 7200
        now[0] = 1180.0
        assert runtime.owners.agent_runtime.revise_human_request(original["request_ref"], **command) == revised
    finally:
        runtime.close()


@pytest.mark.parametrize("decision", ["declined", "deferred", "provided"])
def test_non_approval_response_does_not_change_approved_budget(tmp_path, decision):
    runtime = _runtime(tmp_path / decision)
    try:
        ae = runtime.owners.advancement_engine
        quest = _confirm_direct_quest(runtime)["quest_ref"]
        before = ae.query_research_time(quest)
        request = _open_extension(runtime, quest, before["budget_revision"])
        response = runtime.owners.human_collaboration.respond_to_human_request(
            request["request_ref"], decision=decision, facts={}, note="No explicit allowance approved.",
            idempotency_key="non-approval")
        after = ae.query_research_time(quest)
        assert after["approved_seconds"] == before["approved_seconds"]
        assert after["budget_revision"] == before["budget_revision"]
        assert "time_budget_approval" not in response
    finally:
        runtime.close()


@pytest.mark.parametrize("decision", ["declined", "deferred"])
def test_declined_or_deferred_extension_returns_decision_to_original_root(tmp_path, decision):
    runtime = _root_runtime(tmp_path / ("root-" + decision))
    try:
        run, channel = _bundle_root_channel(runtime)
        ar = runtime.owners.agent_runtime
        quest = ar.query_managed_run(run.run_ref)["quest_ref"]
        before = runtime.owners.advancement_engine.query_research_time(quest)
        with TestClient(create_app(runtime, base_url="http://testserver", control_key="control")) as client:
            headers = _initialized_child(client, channel.connection.token)
            opened = _structured(_tool_call(client, headers,
                operation_id=ROOT_AGENT_HUMAN_REQUEST_OPERATION_IDS[0], request_id=10, arguments={
                    "effect_id": "decline-extension", "request_kind": "research_time_extension",
                    "obligation": "Allow verification time.", "business_purpose": "Need one more verification hour.",
                    "condition": {"impact": "Otherwise close with current uncertainty.",
                        "safe_response": "Approve an allowance or decline.", "time_extension": {
                            "budget_revision": before["budget_revision"], "requested_additional_seconds": 3600}},
                    "acceptance_conditions": ["A human-approved allowance is applied."],
                }))
            request_ref = opened["request_ref"]
            runtime.owners.human_collaboration.respond_to_human_request(request_ref,
                decision=decision, facts={}, note="Continue only with orderly closure.", idempotency_key=decision)
            after = ar.query_human_request(request_ref)
            assert after["status"] == "unsatisfied"
            assert after["direct_waiters"][0]["status"] == "consumed"
            resumed = ar.query_managed_run(run.run_ref)
            assert resumed["root_session_ref"] == run.root_session_ref
            assert resumed["status"] == "running"
            budget = runtime.owners.advancement_engine.query_research_time(quest)
            assert budget["budget_revision"] == before["budget_revision"]
            assert budget["approved_seconds"] == before["approved_seconds"]
            reconciled = _structured(_tool_call(client, headers,
                operation_id=ROOT_AGENT_HUMAN_REQUEST_OPERATION_IDS[1],
                arguments={"effect_id": "decline-extension"}, request_id=11))
            assert reconciled["resolution"]["decision"] == decision
            assert "time_budget_approval" not in reconciled["resolution"]
    finally:
        runtime.close()


def test_extension_handoff_preserves_requested_time_and_original_budget_observation(tmp_path, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr("time.time", lambda: now[0])
    runtime = _runtime(tmp_path / "extension-handoff")
    try:
        ae = runtime.owners.advancement_engine
        ae.start_research_time_service()
        quest = _confirm_direct_quest(runtime)["quest_ref"]
        now[0] = 1060.0
        request = _open_extension(runtime, quest, ae.query_research_time(quest)["budget_revision"])
        now[0] = 1180.0
        client, _ = _authenticated_client(runtime)
        with client:
            result = client.get(f"/api/v1/human-requests/{quote(request['request_ref'], safe='')}/handoff",
                                params={"revision": 1})
            assert result.status_code == 200
            sections = {section["key"]: section["paragraphs"] for section in result.json()["sections"]}
            text = "\n".join(sections["research_time"])
            assert "申请额外 1 小时" in text
            assert "已批准总时长：168 小时" in text
            assert "已用：1 分钟" in text
            assert "等待回复仍计时" in text
            assert runtime.owners.agent_runtime.query_human_request(request["request_ref"]) == request
    finally:
        runtime.close()
